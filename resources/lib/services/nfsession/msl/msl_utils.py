# -*- coding: utf-8 -*-
"""
    Copyright (C) 2017 Sebastian Golasch (plugin.video.netflix)
    Copyright (C) 2019 Stefano Gottardo - @CastagnaIT (original implementation module)
    MSL utils

    SPDX-License-Identifier: MIT
    See LICENSES/MIT.md for more information.
"""
import json
import random
import time
from functools import wraps
from urllib.parse import urlencode

import xbmcgui

import resources.lib.kodi.ui as ui
from resources.lib import common
from resources.lib.common.exceptions import MSLError
from resources.lib.database.db_utils import TABLE_SESSION
from resources.lib.globals import G
from resources.lib.utils.esn import get_esn

CHROME_BASE_URL = 'https://www.netflix.com/nq/msl_v1/cadmium/'
# 16/10/2020 There is a new api endpoint to now used only for events/logblobs
CHROME_PLAYAPI_URL = 'https://www.netflix.com/msl/playapi/cadmium/'

ENDPOINTS = {
    'manifest_v1': CHROME_BASE_URL + 'pbo_manifests/%5E1.0.0/router',  # "pbo_manifests/^1.0.0/router"
    'manifest': CHROME_PLAYAPI_URL + 'licensedmanifest',
    'license': CHROME_BASE_URL + 'pbo_licenses/%5E1.0.0/router',
    'events': CHROME_PLAYAPI_URL + 'event/1',
    'logblobs': CHROME_PLAYAPI_URL + 'logblob/1'
}

MSL_DATA_FILENAME = 'msl_data.json'

EVENT_START = 'start'      # events/start : Video starts
EVENT_STOP = 'stop'        # events/stop : Video stops
EVENT_KEEP_ALIVE = 'keepAlive'  # events/keepAlive : Update progress status
EVENT_ENGAGE = 'engage'    # events/engage : After user interaction (before stop, on skip, on pause)
EVENT_BIND = 'bind'        # events/bind : ?

AUDIO_CHANNELS_CONV = {1: '1.0', 2: '2.0', 6: '5.1', 8: '7.1'}


def display_error_info(func):
    """Decorator that catches errors raise by the decorated function,
    displays an error info dialog in the UI and re-raises the error"""
    # (Show the error to the user before canceling the response to InputStream Adaptive callback)
    # pylint: disable=missing-docstring
    @wraps(func)
    def error_catching_wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            message = f'{exc.__class__.__name__}: {exc}'
            ui.show_error_info(common.get_local_string(30028), message,
                               unknown_error=not message,
                               netflix_error=isinstance(exc, MSLError))
            raise
    return error_catching_wrapper


def is_media_changed(previous_player_state, player_state):
    """Check if there are variations in player state to avoids overhead processing"""
    if not previous_player_state:
        return True
    # To now we do not check subtitle, because to the moment it is not implemented
    if player_state['currentvideostream'] != previous_player_state['currentvideostream'] or \
            player_state['currentaudiostream'] != previous_player_state['currentaudiostream']:
        return True
    return False


def update_play_times_duration(play_times, player_state):
    """Update the playTimes duration values"""
    duration = player_state['elapsed_seconds'] * 1000
    play_times['total'] = duration
    play_times['audio'][0]['duration'] = duration
    play_times['video'][0]['duration'] = duration


def build_media_tag(player_state, manifest, position):
    """Build the playTimes and the mediaId data by parsing manifest and the current player streams used"""
    common.apply_lang_code_changes(manifest['audio_tracks'])

    audio_downloadable_id, audio_track_id = _find_audio_data(player_state, manifest)
    video_downloadable_id, video_track_id = _find_video_data(player_state, manifest)
    text_track_id = _find_subtitle_data(manifest)
    duration = position - 1  # 22//11/2021 The duration has the subtracted value of 1
    play_times = {
        'total': duration,
        'audio': [{
            'downloadableId': audio_downloadable_id,
            'duration': duration
        }],
        'video': [{
            'downloadableId': video_downloadable_id,
            'duration': duration
        }],
        'text': []
    }
    return play_times, video_track_id, audio_track_id, text_track_id


def _find_audio_data(player_state, manifest):
    """
    Find the audio downloadable id and the audio track id
    """
    language = common.convert_language_iso(player_state['currentaudiostream']['language'])
    if not language:  # If there is no language, means that is a fixed locale (fix_locale_languages in kodi_ops.py)
        language = player_state['currentaudiostream']['language']
    channels = AUDIO_CHANNELS_CONV[player_state['currentaudiostream']['channels']]
    for audio_track in manifest['audio_tracks']:
        if audio_track['language'] == language and audio_track['channels'] == channels:
            # Get the stream dict with the highest bitrate
            stream = max(audio_track['streams'], key=lambda x: x['bitrate'])
            return stream['downloadable_id'], audio_track['new_track_id']
    # Not found?
    raise Exception(
        f'build_media_tag: unable to find audio data with language: {language}, channels: {channels}'
    )


def _find_video_data(player_state, manifest):
    """
    Find the best match for the video downloadable id and the video track id
    """
    codec = player_state['currentvideostream']['codec']
    width = player_state['currentvideostream']['width']
    height = player_state['currentvideostream']['height']
    for video_track in manifest['video_tracks']:
        for stream in video_track['streams']:
            if codec in stream['content_profile'] and width == stream['res_w'] and height == stream['res_h']:
                return stream['downloadable_id'], video_track['new_track_id']
    # Not found?
    raise Exception(
        f'build_media_tag: unable to find video data with codec: {codec}, width: {width}, height: {height}'
    )


def _find_subtitle_data(manifest):
    """
    Find the subtitle downloadable id and the subtitle track id
    """
    # Todo: is needed to implement the code to find the appropriate data
    #       for now we always return the "disabled" track
    for sub_track in manifest['timedtexttracks']:
        if sub_track['isNoneTrack']:
            return sub_track['new_track_id']
    # Not found?
    raise Exception('build_media_tag: unable to find subtitle data')


def generate_logblobs_params():
    """Generate the initial log blog"""
    # It seems that this log is sent when logging in to a profile the first time
    # i think it is the easiest to reproduce, the others contain too much data
    screen_size = f'{xbmcgui.getScreenWidth()}x{xbmcgui.getScreenHeight()}'
    timestamp_utc = time.time()
    timestamp = int(timestamp_utc * 1000)
    app_id = int(time.time()) * 10000 + random.SystemRandom().randint(1, 10001)  # Should be used with all log requests

    # Here you have to enter only the real data, falsifying the data would cause repercussions in netflix server logs
    # therefore since it is possible to exclude data, we avoid entering data that we do not have
    blob = {
        'browserua': common.get_user_agent().replace(' ', '#'),
        'browserhref': 'https://www.netflix.com/browse',
        # 'initstart': 988,
        # 'initdelay': 268,
        'screensize': screen_size,  # '1920x1080',
        'screenavailsize': screen_size,  # '1920x1040',
        'clientsize': screen_size,  # '1920x944',
        # 'pt_navigationStart': -1880,
        # 'pt_fetchStart': -1874,
        # 'pt_secureConnectionStart': -1880,
        # 'pt_requestStart': -1853,
        # 'pt_domLoading': -638,
        # 'm_asl_start': 990,
        # 'm_stf_creat': 993,
        # 'm_idb_open': 993,
        # 'm_idb_succ': 1021,
        # 'm_msl_load_no_data': 1059,
        # 'm_asl_comp': 1256,
        'type': 'startup',
        'sev': 'info',
        'devmod': 'chrome-cadmium',
        'clver': G.LOCAL_DB.get_value('client_version', '', table=TABLE_SESSION),  # e.g. '6.0021.220.051'
        'osplatform': G.LOCAL_DB.get_value('browser_info_os_name', '', table=TABLE_SESSION),
        'osver': G.LOCAL_DB.get_value('browser_info_os_version', '', table=TABLE_SESSION),
        'browsername': 'Chrome',
        'browserver': G.LOCAL_DB.get_value('browser_info_version', '', table=TABLE_SESSION),
        'appLogSeqNum': 0,
        'uniqueLogId': common.get_random_uuid(),
        'appId': app_id,
        'esn': get_esn(),
        'lver': '',
        # 'jssid': '15822792997793',  # Same value of appId
        # 'jsoffms': 1261,
        'clienttime': timestamp,
        'client_utc': int(timestamp_utc),
        'uiver': G.LOCAL_DB.get_value('ui_version', '', table=TABLE_SESSION)
    }

    blobs_container = {
        'entries': [blob]
    }
    blobs_dump = json.dumps(blobs_container)
    blobs_dump = blobs_dump.replace('"', '\"').replace(' ', '').replace('#', ' ')
    return {'logblobs': blobs_dump}


def create_req_params(req_name):
    """Create the params for the POST request"""
    # Used list of tuple in order to preserve the params order
    # Will result something like:
    # ?reqAttempt=&reqName=events/engage&clienttype=akira&uiversion=vdeb953cf&browsername=chrome&browserversion=84.0.4147.136&osname=windows&osversion=10.0
    params = [
        ('reqAttempt', ''),  # Will be populated by _post() in msl_requests.py
        ('reqName', req_name),
        ('clienttype', 'akira'),
        ('uiversion', G.LOCAL_DB.get_value('build_identifier', '', table=TABLE_SESSION)),
        ('browsername', 'chrome'),
        ('browserversion', G.LOCAL_DB.get_value('browser_info_version', '', table=TABLE_SESSION).lower()),
        ('osname', G.LOCAL_DB.get_value('browser_info_os_name', '', table=TABLE_SESSION).lower()),
        ('osversion', G.LOCAL_DB.get_value('browser_info_os_version', '', table=TABLE_SESSION))
    ]
    return f'?{urlencode(params)}'
