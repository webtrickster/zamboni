from django.core.management.base import BaseCommand

from celery.task.sets import TaskSet

from amo.utils import chunked
from devhub.models import ActivityLog
from editors.tasks import add_versionlog


class Command(BaseCommand):
    help = 'Add a VersionLog entry for all ActivityLog items'

    def handle(self, *args, **options):
        pks = (ActivityLog.objects.review_queue().values_list('pk', flat=True)
                                  .order_by('id'))

        ts = [add_versionlog.subtask(args=[chunk])
              for chunk in chunked(pks, 100)]
        TaskSet(ts).apply_async()
