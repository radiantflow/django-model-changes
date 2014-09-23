from django.db.models.signals import ModelSignal


post_change =  ModelSignal(providing_args=["instance", "changes"], use_caching=True)
"""
Signal sent whenever an instance is saved or deleted
and changes have been recorded.
"""
