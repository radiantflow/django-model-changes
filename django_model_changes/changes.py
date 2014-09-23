from django.db.models import signals
from django.db.models.base import ModelBase
from django.core.exceptions import ImproperlyConfigured

from .signals import post_change

SAVE = 0
DELETE = 1


class AlreadyRegistered(Exception):
    pass


class NotRegistered(Exception):
    pass


class ModelChangesRegistry(object):

    def __init__(self):
        self._registry = {}


    def register(self, model_or_iterable, allow_existing=True, **options):
        """
        Registers the given model(s) with the changes manager

        The model(s) should be Model classes, not instances.

        If a model is already registered, this will raise AlreadyRegistered.

        If a model is abstract, this will raise ImproperlyConfigured.
        """

        if isinstance(model_or_iterable, ModelBase):
            model_or_iterable = [model_or_iterable]
        for model in model_or_iterable:
            if model._meta.abstract:
                raise ImproperlyConfigured('The model %s is abstract, so it '
                      'cannot be registered with model changes.' % model.__name__)

            if model in self._registry and not allow_existing:
                raise AlreadyRegistered('The model %s is already registered' % model.__name__)
            else:
                # Instantiate the changes class to save in the registry
                self._registry[model] = ModelChanges(model, self)

    def unregister(self, model_or_iterable):
        """
        Unregisters the given model(s).

        If a model isn't already registered, this will raise NotRegistered.
        """
        if isinstance(model_or_iterable, ModelBase):
            model_or_iterable = [model_or_iterable]
        for model in model_or_iterable:
            if model not in self._registry:
                raise NotRegistered('The model %s is not registered' % model.__name__)
            del self._registry[model]


class ModelChanges(object):
    """
    ModelChanges handles signals for a specified  model

    """

    def __init__(self, model, registry):

        self.model = model
        self._registry = registry

        signals.post_init.connect(
            self._post_init, sender=model,
            dispatch_uid='django-changes-%s' % str(model._meta)
        )
        signals.post_save.connect(
            self._post_save, sender=model,
            dispatch_uid='django-changes-%s' % str(model._meta)
        )
        signals.post_delete.connect(
            self._post_delete, sender=model,
            dispatch_uid='django-changes-%s' % str(model._meta)
        )


    def _post_init(self, sender, instance, **kwargs):
        instance._changes = InstanceChanges(instance, self)

    def _post_save(self, sender, instance, **kwargs):
        if hasattr(instance, '_changes'):
            instance._changes._save_state(new_instance=False, event_type=SAVE)

    def _post_delete(self, sender, instance, **kwargs):
        if hasattr(instance, '_changes'):
            instance._save_state(new_instance=False, event_type=DELETE)




class InstanceChanges(object):
    """
    InstanceChanges keeps track of changes for model instances.

    It allows you to retrieve the following states from an instance:

    1. current_state()
        The current state of the instance.
    2. previous_state()
        The state of the instance **after** it was created, saved
        or deleted the last time.
    3. old_state()
        The previous previous_state(), i.e. the state of the
        instance **before** it was created, saved or deleted the
        last time.

    It also provides convenience methods to get changes between states:

    1. changes()
        Changes from previous_state to current_state.
    2. previous_changes()
        Changes from old_state to previous_state.
    3. old_changes()
        Changes from old_state to current_state.

    And the following methods to determine if an instance was/is persisted in
    the database:

    1. was_persisted()
        Was the instance persisted in its old state.
    2. is_persisted()
        Is the instance is_persisted in its current state.

    This schematic tries to illustrate how these methods relate to
    each other::


        after create/save/delete            after save/delete                  now
        |                                   |                                  |
        .-----------------------------------.----------------------------------.
        |\                                  |\                                 |\
        | \                                 | \                                | \
        |  old_state()                      |  previous_state()                |  current_state()
        |                                   |                                  |
        |-----------------------------------|----------------------------------|
        |  previous_changes() (prev - old)  |  changes() (cur - prev)          |
        |-----------------------------------|----------------------------------|
        |                      old_changes()  (cur - old)                      |
        .----------------------------------------------------------------------.
         \                                                                      \
          \                                                                      \
           was_persisted()                                                        is_persisted()

    """

    def __init__(self, instance, model_changes):
        self.instance = instance
        self.model_changes = model_changes
        self.model = model_changes.model
        self._states = []
        self._save_state(new_instance=True)


    def _save_state(self, new_instance=False, event_type='save'):
        # Pipe the pk on deletes so that a correct snapshot of the current
        # state can be taken.
        if event_type == DELETE:
            self.instance.pk = None

        # Save current state.
        self._states.append(self.current_state())

        # Drop the previous old state
        # _states == [previous old state, old state, previous state]
        #             ^^^^^^^^^^^^^^^^^^
        if len(self._states) > 2:
            self._states.pop(0)

        # Send post_change signal unless this is a new instance
        if not new_instance:
            post_change.send(sender=self.instance.__class__, instance=self.instance, changes=self)

    def _instance_from_state(self, state):
        """
        Creates an instance from a previously saved state.
        """
        instance = self.instance.__class__()
        for key, value in state.items():
            setattr(instance, key, value)
        return instance

    def current_state(self):
        """
        Returns a ``field -> value`` dict of the current state of the instance.
        """
        fields = {}
        for field in self.instance._meta.local_fields:
            # It's always safe to access the field attribute name, it refers to simple types that are immediately
            # available on the instance.
            fields[field.attname] = getattr(self.instance, field.attname)

            # Foreign fields require special care because we don't want to trigger a database query when the field is
            # not yet cached.
            if field.rel:
                descriptor = self.instance.__class__.__dict__[field.name]
                if hasattr(self.instance, descriptor.cache_name):
                    fields[field.name] = getattr(self.instance, descriptor.cache_name)

        return fields

    def previous_state(self):
        """
        Returns a ``field -> value`` dict of the state of the instance after it
        was created, saved or deleted the previous time.
        """
        if len(self._states) > 1:
            return self._states[1]
        else:
            return self._states[0]

    def old_state(self):
        """
        Returns a ``field -> value`` dict of the state of the instance after
        it was created, saved or deleted the previous previous time. Returns
        the previous state if there is no previous previous state.
        """
        return self._states[0]

    def _changes(self, other, current):
        return dict([(key, (was, current[key])) for key, was in other.iteritems() if was != current[key]])

    def changes(self):
        """
        Returns a ``field -> (previous value, current value)`` dict of changes
        from the previous state to the current state.
        """
        return self._changes(self.previous_state(), self.current_state())

    def old_changes(self):
        """
        Returns a ``field -> (previous value, current value)`` dict of changes
        from the old state to the current state.
        """
        return self._changes(self.old_state(), self.current_state())

    def previous_changes(self):
        """
        Returns a ``field -> (previous value, current value)`` dict of changes
        from the old state to the previous state.
        """
        return self._changes(self.old_state(), self.previous_state())

    def was_persisted(self):
        """
        Returns true if the instance was persisted (saved) in its old
        state.

        Examples::

            >>> user = User()
            >>> user.save()
            >>> user.was_persisted()
            False

            >>> user = User.objects.get(pk=1)
            >>> user.delete()
            >>> user.was_persisted()
            True
        """
        pk_attname = self.instance._meta.pk.attname
        return bool(self.old_state()[pk_attname])

    def is_persisted(self):
        """
        Returns true if the instance is persisted (saved) in its current
        state.

        Examples:

            >>> user = User()
            >>> user.save()
            >>> user.is_persisted()
            True

            >>> user = User.objects.get(pk=1)
            >>> user.delete()
            >>> user.is_persisted()
            False
        """
        return bool(self.instance.pk)

    def old_instance(self):
        """
        Returns an instance of this model in its old state.
        """
        return self._instance_from_state(self.old_state())

    def previous_instance(self):
        """
        Returns an instance of this model in its previous state.
        """
        return self._instance_from_state(self.previous_state())


registry = ModelChangesRegistry()
