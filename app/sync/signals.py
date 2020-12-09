from django.conf import settings
from django.db.models.signals import pre_save, post_save, pre_delete, post_delete
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _
from background_task.signals import task_failed
from background_task.models import Task
from common.logger import log
from .models import Source, Media
from .tasks import (delete_task_by_source, delete_task_by_media, index_source_task,
                    download_media_thumbnail, map_task_to_instance,
                    check_source_directory_exists, download_media)
from .utils import delete_file


@receiver(pre_save, sender=Source)
def source_pre_save(sender, instance, **kwargs):
    # Triggered before a source is saved, if the schedule has been updated recreate
    # its indexing task
    try:
        existing_source = Source.objects.get(pk=instance.pk)
    except Source.DoesNotExist:
        # Probably not possible?
        return
    if existing_source.index_schedule != instance.index_schedule:
        # Indexing schedule has changed, recreate the indexing task
        delete_task_by_source('sync.tasks.index_source_task', instance.pk)
        verbose_name = _('Index media from source "{}"')
        index_source_task(
            str(instance.pk),
            repeat=instance.index_schedule,
            queue=str(instance.pk),
            verbose_name=verbose_name.format(instance.name)
        )


@receiver(post_save, sender=Source)
def source_post_save(sender, instance, created, **kwargs):
    # Triggered after a source is saved, Create a new task to check the directory exists
    check_source_directory_exists(str(instance.pk))
    if created:
        # Create a new indexing task for newly created sources
        delete_task_by_source('sync.tasks.index_source_task', instance.pk)
        log.info(f'Scheduling media indexing for source: {instance.name}')
        verbose_name = _('Index media from source "{}"')
        index_source_task(
            str(instance.pk),
            repeat=instance.index_schedule,
            queue=str(instance.pk),
            verbose_name=verbose_name.format(instance.name)
        )
    # Trigger the post_save signal for each media item linked to this source as various
    # flags may need to be recalculated
    for media in Media.objects.filter(source=instance):
        media.save()


@receiver(pre_delete, sender=Source)
def source_pre_delete(sender, instance, **kwargs):
    # Triggered before a source is deleted, delete all media objects to trigger
    # the Media models post_delete signal
    for media in Media.objects.filter(source=instance):
        log.info(f'Deleting media for source: {instance.name} item: {media.name}')
        media.delete()


@receiver(post_delete, sender=Source)
def source_post_delete(sender, instance, **kwargs):
    # Triggered after a source is deleted
    log.info(f'Deleting tasks for source: {instance.name}')
    delete_task_by_source('sync.tasks.index_source_task', instance.pk)


@receiver(task_failed, sender=Task)
def task_task_failed(sender, task_id, completed_task, **kwargs):
    # Triggered after a task fails by reaching its max retry attempts
    obj, url = map_task_to_instance(completed_task)
    if isinstance(obj, Source):
        log.error(f'Permanent failure for source: {obj} task: {completed_task}')
        obj.has_failed = True
        obj.save()


@receiver(post_save, sender=Media)
def media_post_save(sender, instance, created, **kwargs):
    # Triggered after media is saved
    if created:
        # If the media is newly created start a task to download its thumbnail
        thumbnail_url = instance.thumbnail
        if thumbnail_url:
            log.info(f'Scheduling task to download thumbnail for: {instance.name} '
                     f'from: {thumbnail_url}')
            verbose_name = _('Downloading thumbnail for "{}"')
            download_media_thumbnail(
                str(instance.pk),
                thumbnail_url,
                queue=str(instance.source.pk),
                verbose_name=verbose_name.format(instance.name)
            )
    # Recalculate the "can_download" flag, this may need to change if the source
    # specifications have been changed
    if instance.get_format_str():
        if not instance.can_download:
            instance.can_download = True
            instance.save()
    else:
        if instance.can_download:
            instance.can_download = True
            instance.save()
    # If the media has not yet been downloaded schedule it to be downloaded
    if not instance.downloaded:
        delete_task_by_media('sync.tasks.download_media', (str(instance.pk),))
        verbose_name = _('Downloading media for "{}"')
        download_media(
            str(instance.pk),
            queue=str(instance.source.pk),
            verbose_name=verbose_name.format(instance.name)
        )


@receiver(pre_delete, sender=Media)
def media_pre_delete(sender, instance, **kwargs):
    # Triggered before media is deleted, delete any scheduled tasks
    log.info(f'Deleting tasks for media: {instance.name}')
    delete_task_by_media('sync.tasks.download_media', (str(instance.pk),))
    thumbnail_url = instance.thumbnail
    if thumbnail_url:
        delete_task_by_media('sync.tasks.download_media_thumbnail',
                             (str(instance.pk), thumbnail_url))
    # Delete media thumbnail if it exists
    if instance.thumb:
        log.info(f'Deleting thumbnail for: {instance} path: {instance.thumb.path}')
        delete_file(instance.thumb.path)