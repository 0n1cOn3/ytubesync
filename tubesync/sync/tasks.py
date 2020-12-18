'''
    Start, stop and manage scheduled tasks. These are generally triggered by Django
    signals (see signals.py).
'''


import os
import json
import math
import uuid
from io import BytesIO
from hashlib import sha1
from datetime import timedelta
from shutil import copyfile
from PIL import Image
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from django.db.utils import IntegrityError
from django.utils.translation import gettext_lazy as _
from background_task import background
from background_task.models import Task, CompletedTask
from common.logger import log
from common.errors import NoMediaException, DownloadFailedException
from .models import Source, Media, MediaServer
from .utils import get_remote_image, resize_image_to_height, delete_file


def get_hash(task_name, pk):
    '''
        Create a background_task compatible hash for a Task or CompletedTask.
    '''
    task_params = json.dumps(((str(pk),), {}), sort_keys=True)
    return sha1(f'{task_name}{task_params}'.encode('utf-8')).hexdigest()


def map_task_to_instance(task):
    '''
        Reverse-maps a scheduled backgrond task to an instance. Requires the task name
        to be a known task function and the first argument to be a UUID. This is used
        because UUID's are incompatible with background_task's "creator" feature.
    '''
    TASK_MAP = {
        'sync.tasks.index_source_task': Source,
        'sync.tasks.check_source_directory_exists': Source,
        'sync.tasks.download_media_thumbnail': Media,
        'sync.tasks.download_media': Media,
    }
    MODEL_URL_MAP = {
        Source: 'sync:source',
        Media: 'sync:media-item',
    }
    # Unpack 
    task_func, task_args_str = task.task_name, task.task_params
    model = TASK_MAP.get(task_func, None)
    if not model:
        return None, None
    url = MODEL_URL_MAP.get(model, None)
    if not url:
        return None, None
    try:
        task_args = json.loads(task_args_str)
    except (TypeError, ValueError, AttributeError):
        return None, None
    if len(task_args) != 2:
        return None, None
    args, kwargs = task_args
    if len(args) == 0:
        return None, None
    instance_uuid_str = args[0]
    try:
        instance_uuid = uuid.UUID(instance_uuid_str)
    except (TypeError, ValueError, AttributeError):
        return None, None
    try:
        instance = model.objects.get(pk=instance_uuid)
        return instance, url
    except model.DoesNotExist:
        return None, None


def get_error_message(task):
    '''
        Extract an error message from a failed task. This is the last line of the
        last_error field with the method name removed.
    '''
    if not task.has_error():
        return ''
    stacktrace_lines = task.last_error.strip().split('\n')
    if len(stacktrace_lines) == 0:
        return ''
    error_message = stacktrace_lines[-1].strip()
    if ':' not in error_message:
        return ''
    return error_message.split(':', 1)[1].strip()


def get_source_completed_tasks(source_id, only_errors=False):
    '''
        Returns a queryset of CompletedTask objects for a source by source ID.
    '''
    q = {'queue': source_id}
    if only_errors:
        q['failed_at__isnull'] = False
    return CompletedTask.objects.filter(**q).order_by('-failed_at')


def get_media_download_task(media_id):
    try:
        return Task.objects.get_task('sync.tasks.download_media',
                                     args=(str(media_id),))[0]
    except IndexError:
        return False


def delete_task_by_source(task_name, source_id):
    return Task.objects.filter(task_name=task_name, queue=str(source_id)).delete()


def delete_task_by_media(task_name, args):
    return Task.objects.drop_task(task_name, args=args)


def cleanup_completed_tasks():
    days_to_keep = getattr(settings, 'COMPLETED_TASKS_DAYS_TO_KEEP', 30)
    delta = timezone.now() - timedelta(days=days_to_keep)
    log.info(f'Deleting completed tasks older than {days_to_keep} days '
             f'(run_at before {delta})')
    CompletedTask.objects.filter(run_at__lt=delta).delete()


def cleanup_old_media():
    for media in Media.objects.filter(download_date__isnull=False):
        if media.source.delete_old_media and media.source.days_to_keep > 0:
            delta = timezone.now() - timedelta(days=media.source.days_to_keep)
            if media.downloaded and media.download_date < delta:
                # Media was downloaded after the cutoff date, delete it
                log.info(f'Deleting expired media: {media.source} / {media} '
                         f'(now older than {media.source.days_to_keep} days / '
                         f'download_date before {delta})')
                # .delete() also triggers a pre_delete signal that removes the files
                media.delete()


@background(schedule=0)
def index_source_task(source_id):
    '''
        Indexes media available from a Source object.
    '''
    try:
        source = Source.objects.get(pk=source_id)
    except Source.DoesNotExist:
        # Task triggered but the Source has been deleted, delete the task
        delete_index_source_task(source_id)
        return
    # Reset any errors
    source.has_failed = False
    source.save()
    # Index the source
    videos = source.index_media()
    if not videos:
        raise NoMediaException(f'Source "{source}" (ID: {source_id}) returned no '
                               f'media to index, is the source key valid? Check the '
                               f'source configuration is correct and that the source '
                               f'is reachable')
    # Got some media, update the last crawl timestamp
    source.last_crawl = timezone.now()
    source.save()
    log.info(f'Found {len(videos)} media items for source: {source}')
    for video in videos:
        # Create or update each video as a Media object
        key = video.get(source.key_field, None)
        if not key:
            # Video has no unique key (ID), it can't be indexed
            continue
        try:
            media = Media.objects.get(key=key)
        except Media.DoesNotExist:
            media = Media(key=key)
        media.source = source
        media.metadata = json.dumps(video)
        upload_date = media.upload_date
        # Media must have a valid upload date
        if upload_date:
            media.published = timezone.make_aware(upload_date)
        else:
            log.error(f'Media has no upload date, skipping: {source} / {media}')
            continue
        # If the source has a cut-off check the upload date is within the allowed delta
        if source.delete_old_media and source.days_to_keep > 0:
            delta = timezone.now() - timedelta(days=source.days_to_keep)
            if media.published < delta:
                # Media was published after the cutoff date, skip it
                log.warn(f'Media: {source} / {media} is older than '
                         f'{source.days_to_keep} days, skipping')
                continue
        try:
            media.save()
            log.info(f'Indexed media: {source} / {media}')
        except IntegrityError as e:
            log.error(f'Index media failed: {source} / {media} with "{e}"')
    # Tack on a cleanup of old completed tasks
    cleanup_completed_tasks()
    # Tack on a cleanup of old media
    cleanup_old_media()


@background(schedule=0)
def check_source_directory_exists(source_id):
    '''
        Checks the output directory for a source exists and is writable, if it does
        not attempt to create it. This is a task so if there are permission errors
        they are logged as failed tasks.
    '''
    try:
        source = Source.objects.get(pk=source_id)
    except Source.DoesNotExist:
        # Task triggered but the Source has been deleted, delete the task
        delete_index_source_task(source_id)
        return
    # Check the source output directory exists
    if not source.directory_exists():
        # Try and create it
        log.info(f'Creating directory: {source.directory_path}')
        source.make_directory()


@background(schedule=0)
def download_media_thumbnail(media_id, url):
    '''
        Downloads an image from a URL and save it as a local thumbnail attached to a
        Media instance.
    '''
    try:
        media = Media.objects.get(pk=media_id)
    except Media.DoesNotExist:
        # Task triggered but the media no longer exists, do nothing
        return
    width = getattr(settings, 'MEDIA_THUMBNAIL_WIDTH', 430)
    height = getattr(settings, 'MEDIA_THUMBNAIL_HEIGHT', 240)
    i = get_remote_image(url)
    log.info(f'Resizing {i.width}x{i.height} thumbnail to '
             f'{width}x{height}: {url}')
    i = resize_image_to_height(i, width, height)
    image_file = BytesIO()
    i.save(image_file, 'JPEG', quality=85, optimize=True, progressive=True)
    image_file.seek(0)
    media.thumb.save(
        'thumb',
        SimpleUploadedFile(
            'thumb',
            image_file.read(),
            'image/jpeg',
        ),
        save=True
    )
    log.info(f'Saved thumbnail for: {media} from: {url}')
    return True


@background(schedule=0)
def download_media(media_id):
    '''
        Downloads the media to disk and attaches it to the Media instance.
    '''
    try:
        media = Media.objects.get(pk=media_id)
    except Media.DoesNotExist:
        # Task triggered but the media no longer exists, do nothing
        return
    if media.skip:
        # Media was toggled to be skipped after the task was scheduled
        log.warn(f'Download task triggeredd media: {media} (UUID: {media.pk}) but it '
                 f'is now marked to be skipped, not downloading')
        return
    filepath = media.filepath
    log.info(f'Downloading media: {media} (UUID: {media.pk}) to: "{filepath}"')
    format_str, container = media.download_media()
    if os.path.exists(filepath):
        # Media has been downloaded successfully
        log.info(f'Successfully downloaded media: {media} (UUID: {media.pk}) to: '
                 f'"{filepath}"')
        # Link the media file to the object and update info about the download
        media.media_file.name = str(filepath)
        media.downloaded = True
        media.download_date = timezone.now()
        media.downloaded_filesize = os.path.getsize(filepath)
        media.downloaded_container = container
        if '+' in format_str:
            # Seperate audio and video streams
            vformat_code, aformat_code = format_str.split('+')
            aformat = media.get_format_by_code(aformat_code)
            vformat = media.get_format_by_code(vformat_code)
            media.downloaded_format = vformat['format']
            media.downloaded_height = vformat['height']
            media.downloaded_width = vformat['width']
            media.downloaded_audio_codec = aformat['acodec']
            media.downloaded_video_codec = vformat['vcodec']
            media.downloaded_container = container
            media.downloaded_fps = vformat['fps']
            media.downloaded_hdr = vformat['is_hdr']
        else:
            # Combined stream or audio-only stream
            cformat_code = format_str
            cformat = media.get_format_by_code(cformat_code)
            media.downloaded_audio_codec = cformat['acodec']
            if cformat['vcodec']:
                # Combined
                media.downloaded_format = vformat['format']
                media.downloaded_height = cformat['height']
                media.downloaded_width = cformat['width']
                media.downloaded_video_codec = cformat['vcodec']
                media.downloaded_fps = cformat['fps']
                media.downloaded_hdr = cformat['is_hdr']
            else:
                media.downloaded_format = 'audio'
        media.save()
        # If selected, copy the thumbnail over as well
        if media.source.copy_thumbnails and media.thumb:
            barefilepath, fileext = os.path.splitext(filepath)
            thumbpath = f'{barefilepath}.jpg'
            log.info(f'Copying media thumbnail from: {media.thumb.path} '
                     f'to: {thumbpath}')
            copyfile(media.thumb.path, thumbpath)
        # Schedule a task to update media servers
        for mediaserver in MediaServer.objects.all():
            log.info(f'Scheduling media server updates')
            verbose_name = _('Request media server rescan for "{}"')
            rescan_media_server(
                str(mediaserver.pk),
                queue=str(media.source.pk),
                priority=0,
                verbose_name=verbose_name.format(mediaserver),
                remove_existing_tasks=True
            )
    else:
        # Expected file doesn't exist on disk
        err = (f'Failed to download media: {media} (UUID: {media.pk}) to disk, '
               f'expected outfile does not exist: {media.filepath}')
        log.error(err)
        # Raising an error here triggers the task to be re-attempted (or fail)
        raise DownloadFailedException(err)


@background(schedule=0)
def rescan_media_server(mediaserver_id):
    '''
        Attempts to request a media rescan on a remote media server.
    '''
    try:
        mediaserver = MediaServer.objects.get(pk=mediaserver_id)
    except MediaServer.DoesNotExist:
        # Task triggered but the media server no longer exists, do nothing
        return
    # Request an rescan / update
    log.info(f'Updating media server: {mediaserver}')
    mediaserver.update()
