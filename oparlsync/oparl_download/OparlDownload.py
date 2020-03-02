# encoding: utf-8

"""
Copyright (c) 2012 - 2016, Ernesto Ruge
All rights reserved.
Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:
1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.
THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

import re
import os
import sys
import time
import json
import pytz
import urllib
import hashlib
import datetime
import requests
from dateutil.parser import parse as dateutil_parse
from ssl import SSLError
from geojson import Feature
from urllib.parse import urlparse
from ..models import *
from ..base_task import BaseTask
from pymongo import ReturnDocument
from bson.objectid import ObjectId
from minio.error import ResponseError, SignatureDoesNotMatch
from mongoengine.errors import ValidationError
from pymongo.errors import ServerSelectionTimeoutError
from requests.exceptions import ChunkedEncodingError


class OparlDownload(BaseTask):
    name = 'OparlDownload'
    services = [
        'mongodb',
        's3'
    ]
    oparl_version = '1.1'
    modified_since = None
    body_id = None

    def __init__(self, **kwargs):
        super().__init__()

        self.start_time = datetime.datetime.utcnow()
        self.valid_objects = [
            Body,
            LegislativeTerm,
            Organization,
            Person,
            Membership,
            Meeting,
            AgendaItem,
            Consultation,
            Paper,
            File,
            Location
        ]

        # statistics
        self.mongodb_request_count = 0
        self.mongodb_request_cached = 0
        self.http_request_count = 0
        self.mongodb_request_time = 0
        self.file_download_time = 0
        self.download_not_required = 0
        self.http_request_time = 0
        self.minio_time = 0
        self.wait_time = 0

        self.body_uid = False
        self.organization_list_url = False
        self.person_list_url = False
        self.meeting_list_url = False
        self.paper_list_url = False

        self.reset_cache()
        self.modified_since = None
        if kwargs.get('since'):
            self.modified_since = datetime.datetime.strptime(kwargs['since'], '%Y-%m-%d').strftime('%Y-%m-%dT%H:%M:%SZ')
        self.run_full(kwargs.get('body'))
        """
        for arg in args:
            if arg.startswith('since='):
                self.modified_since = dateutil_parse(arg.split('=')[1])
            elif arg.startswith('uid='):
                self.run_single_by_uid(body_id, arg.split('=')[1])
            elif arg.startswith('url='):
                self.run_single_by_url(body_id, arg.split('=')[1])
            elif arg.startswith('list='):
                self.run_single_by_list(body_id, '='.join(arg.split('=')[1:]))
        if len(args):
            if args[0] == 'full':
                self.run_full(body_id, True)
            else:
                self.run_single(body_id, *args)
        else:
            self.run_full(body_id)
        """

    def body_objects(self, last_update):
        result = [
            Organization,
            Person,
            Meeting,
            Paper
        ]
        if self.oparl_version == '1.1' and last_update:
            result += [
                Membership,
                AgendaItem,
                Consultation,
                File,
                Location
            ]
        return result

    def run_full(self, body_id):
        body_config = self.get_body_config(body_id)
        self.datalog.info('Body %s sync launched.' % body_id)
        if not body_config:
            self.datalog.error('body id %s configuration not found' % self.body_id)
            return
        if 'url' not in body_config:
            return
        start_time = time.time()
        body_remote = self.get_body_from_remote(self.body_config['url'])
        local_body = self.update_local_body(body_remote)
        if not local_body:
            return
        last_sync = self.get_last_sync(local_body)
        self.adjust_body_input_before_save(body_remote)
        self.set_oparl_version(body_remote)
        self.save_object(Body, body_remote)

        for object in self.body_objects(last_sync):
            self.get_list(object)

        # set last sync if everything is done so far
        body = Body.objects(id=self.body_uid).first()
        body.beforeLastSync = body.lastSync
        body.lastSync = self.start_time.isoformat()
        body.save()

        self.datalog.info('Body %s sync done. Results:' % self.body_id)
        self.datalog.info('mongodb requests:     %s' % self.mongodb_request_count)
        self.datalog.info('cached requests:      %s' % self.mongodb_request_cached)
        self.datalog.info('http requests:        %s' % self.http_request_count)
        self.datalog.info('mongodb time:         %s s' % round(self.mongodb_request_time, 1))
        self.datalog.info('minio time:           %s s' % round(self.minio_time, 1))
        self.datalog.info('http time:            %s s' % round(self.http_request_time, 1))
        self.datalog.info('file download time:   %s s' % round(self.file_download_time, 1))
        self.datalog.info('download not reqired: %s' % self.download_not_required)
        self.datalog.info('wait time:            %s s' % round(self.wait_time, 1))
        self.datalog.info('app time:             %s s' % round(
            time.time() - start_time - self.mongodb_request_time - self.minio_time - self.http_request_time - self.wait_time - self.file_download_time,
            1
        ))
        self.datalog.info('all time:             %s s' % round(time.time() - start_time, 1))
        self.datalog.info('processed %s objects per second' % round(self.mongodb_request_count / (time.time() - start_time), 1))

    def reset_cache(self):
        self.cache = {}
        for obj in self.valid_objects:
            self.cache[obj.__name__] = {}

    def get_body_from_remote(self, url):
        url_to_use = self.config.OPARL_MIRROR_URL + '/body-by-id?id=' + urllib.parse.quote_plus(url) if self.config.USE_MIRROR else url
        return self.get_url_json(url_to_use, wait=False)

    def create_local_body_input(self, body_remote, body_config):
        object_json = {
            '$set': {
                'rgs': body_config['rgs'],
                'uid': body_config['id']
            }
        }

        if 'legacy' not in body_config:
            object_json['$set']['originalId'] = body_remote[
                self.config.OPARL_MIRROR_PREFIX + ':originalId'] if self.config.USE_MIRROR else body_remote['id']
        if self.config.ENABLE_PROCESSING:
            region = Region.objects(rgs=body_config['rgs']).first()
            if region:
                object_json['$set']['region'] = region.id
        if self.config.USE_MIRROR:
            object_json['$set']['mirrorId'] = body_remote['id']
        self.correct_document_values(object_json['$set'])

        return object_json

    def update_body_mongo(self, input):
        self.mongodb_request_count += 1
        return self.db_raw.body.find_one_and_update(
            {
                'uid': body_config['id']
            },
            input,
            upsert=True,
            return_document=ReturnDocument.AFTER
        )

    def get_last_sync(self, local_body):
        return pytz.UTC.localize(local_body['lastSync']).astimezone(pytz.timezone('Europe/Berlin')) if 'lastSync' in local_body else None

    def set_oparl_version(self, body_remote):
        if body_remote['type'] == 'https://schema.oparl.org/1.0/Body':
            self.oparl_version = '1.0'
        elif body_remote['type'] == 'https://schema.oparl.org/1.1/Body':
            self.oparl_version = '1.1'

    def adjust_body_input_before_save(self, body_remote):
        if not self.config.USE_MIRROR:
            # Copy missing values from system object if necessary
            if not body_remote.get('licence') or not body_remote.get('contactName') or not body_remote.get('contactEmail'):
                system_raw = self.get_url_json(body_remote['system'])
                if system_raw.get('licence') and not body_remote.get('licence'):
                    body_remote['licence'] = system_raw['licence']
                if system_raw.get('contactName') and not body_remote.get('contactName'):
                    body_remote['contactName'] = system_raw['contactName']
                if system_raw.get('contactEmail') and not body_remote.get('contactEmail'):
                    body_remote['contactEmail'] = system_raw['contactEmail']
        return body_remote

    def update_local_body(self, body_remote, set_last_sync=True):
        local_body_input = self.create_local_body_input(body_remote)
        if not local_body_input:
            return
        return self.update_body_mongo(local_body_input)

    def get_list(self, object, list_url=None):
        if list_url:
            object_list = self.get_url_json(list_url, is_list=True)
        else:
            if self.modified_since:
                pass
            elif self.last_update and self.oparl_version == '1.1':
                last_update_tmp = self.last_update
                if self.config.USE_MIRROR:
                    last_update_tmp = last_update_tmp - datetime.timedelta(weeks=1)
                self.modified_since = last_update_tmp.strftime('%Y-%m-%dT%H:%M:%SZ')
            elif self.last_update:
                self.modified_since = (self.last_update - datetime.timedelta(days=90)).strftime('%Y-%m-%dT%H:%M:%SZ')
            url = getattr(self, '%s_list_url' % object._object_db_name)
            if self.modified_since:
                url += '&' if '?' in url else '?'
                url += 'modified_since=%s' % self.modified_since
            object_list = self.get_url_json(url, is_list=True)
        while object_list:
            for object_raw in object_list['data']:
                self.save_object(object, object_raw)
            if 'next' in object_list['links']:
                url = object_list['links']['next']
                # Patching modified_since back in URL because some RIS loose it at page 2 :(
                if 'modified_since' not in url and self.modified_since:
                    url += '&' if '?' in url else '?'
                    url += 'modified_since=%s' % self.modified_since
                object_list = self.get_url_json(url, is_list=True)
            else:
                break

    def save_object(self, object, object_raw, validate=True):
        object_instance = object()
        dbref_data = {}

        # Stupid Bugfix for Person -> Location as locationObject
        if 'locationObject' in object_raw:
            object_raw['location'] = object_raw['locationObject']
            del object_raw['locationObject']

        # Iterate though all Objects and fix stuff (recursive)
        for key, value in object_raw.items():
            if key in object_instance._fields:
                # List of something
                if type(object_instance._fields[key]).__name__ == 'ListField':
                    external_list = False
                    if hasattr(object_instance._fields[key], 'external_list'):
                        if object_instance._fields[key].external_list:
                            continue
                    # List of relations
                    if type(object_instance._fields[key].field).__name__ == 'ReferenceField':
                        dbref_data[key] = []
                        for valid_object in self.valid_objects:
                            check = object_instance._fields[key].field.document_type_obj
                            if type(check) != str:
                                check = check.__name__
                            if valid_object.__name__ == check:
                                for single in value:
                                    if valid_object.__name__ == 'Body':
                                        dbref_data[key].append(ObjectId(self.body_uid))
                                        continue
                                    if isinstance(single, dict) or key == 'derivativeFile':
                                        # we have to get derivativeFile now because it's in no other list
                                        if key == 'derivativeFile':
                                            sub_object_raw = self.get_url_json(single, False)
                                        else:
                                            sub_object_raw = single
                                        if 'created' not in sub_object_raw and 'created' in object_raw:
                                            sub_object_raw['created'] = object_raw['created']
                                        if 'modified' not in sub_object_raw and 'modified' in object_raw:
                                            sub_object_raw['modified'] = object_raw['modified']
                                        dbref_data[key].append(ObjectId(self.save_object(valid_object, sub_object_raw, True)['_id']))
                                    else:
                                        if single in self.cache[valid_object.__name__]:
                                            dbref_data[key].append(self.cache[valid_object.__name__][single])
                                            self.mongodb_request_cached += 1
                                            continue
                                        dbref_data[key].append(ObjectId(self.save_object(valid_object, {'id': single}, False)['_id']))
                    # List of Non-Relation
                    else:
                        self.save_document_values(object_instance, key, value)
                # Single Relation
                elif type(object._fields[key]).__name__ == 'ReferenceField':
                    for valid_object in self.valid_objects:
                        check = object_instance._fields[key].document_type_obj
                        if type(check) != str:
                            check = check.__name__
                        if valid_object.__name__ == check:
                            if valid_object.__name__ == 'Body':
                                dbref_data[key] = ObjectId(self.body_uid)
                                continue
                            # Stupid bugfix for Person -> Location is an object id
                            if object.__name__ == 'Person' and valid_object.__name__ == 'Location' and isinstance(value, str) and not self.config.USE_MIRROR:
                                value = self.get_url_json(value)
                            if isinstance(value, dict):
                                sub_object_raw = value
                                if 'created' not in sub_object_raw and 'created' in object_raw:
                                    sub_object_raw['created'] = object_raw['created']
                                if 'modified' not in sub_object_raw and 'modified' in object_raw:
                                    sub_object_raw['modified'] = object_raw['modified']
                                dbref_data[key] = ObjectId(self.save_object(valid_object, sub_object_raw, True)['_id'])
                            else:
                                if value in self.cache[valid_object.__name__]:
                                    dbref_data[key] = self.cache[valid_object.__name__][value]
                                    self.mongodb_request_cached += 1
                                    continue
                                dbref_data[key] = ObjectId(self.save_object(valid_object, {'id': value}, False)['_id'])
                # No relation or list
                else:
                    self.save_document_values(object_instance, key, value)

        # Validate Object and log invalid objects
        if object != Body and validate:
            try:
                object_instance.validate()
            except ValidationError as err:
                self.datalog.warn(
                    '%s %s from Body %s failed validation.' % (object.__name__, object_raw['id'], self.body_uid))
        # fix modified
        if object_instance.created and object_instance.modified:
            if object_instance.created > object_instance.modified:
                object_instance.modified = object_instance.created

        # Etwas umständlicher Weg über pymongo
        if self.config.USE_MIRROR:
            query = {
                'mirrorId': object_instance.originalId
            }
            object_instance.mirrorId = object_instance.originalId
            if self.config.OPARL_MIRROR_PREFIX + ':originalId' in object_raw:
                object_instance.originalId = object_raw[self.config.OPARL_MIRROR_PREFIX + ':originalId']
            else:
                del object_instance.originalId
        else:
            query = {
                'originalId': object_instance.originalId
            }
        object_json = json.loads(object_instance.to_json())
        for field_key in object_json.keys():
            if type(object_instance._fields[field_key]).__name__ == 'DateTimeField':
                object_json[field_key] = getattr(object_instance, field_key)

        # Body ID related Fixes
        if object == Location:
            object_json['body'] = [self.body_uid]
            if 'geojson' in object_json:
                if 'geometry' in object_json['geojson']:
                    try:
                        geojson_check = Feature(geometry=object_json['geojson']['geometry'])
                        if not geojson_check.is_valid:
                            del object_json['geojson']
                            self.datalog.warn('invalid geojson found at %s' % object_instance.originalId)
                    except ValueError:
                        del object_json['geojson']
                        self.datalog.warn('invalid geojson found at %s' % object_instance.originalId)

        elif object == Body:
            if self.body_config['name']:
                object_json['name'] = self.body_config['name']
        else:
            object_json['body'] = self.body_uid

        # Set some File values if using mirror
        if object == File and self.config.USE_MIRROR:
            object_json['storedAtMirror'] = True
            if 'originalAccessUrl' in object_json:
                object_json['mirrorAccessUrl'] = object_json['originalAccessUrl']
            if 'originalDownloadUrl' in object_json:
                object_json['mirrorDownloadUrl'] = object_json['originalDownloadUrl']
            if self.config.OPARL_MIRROR_PREFIX + ':originalAccessUrl' in object_raw:
                object_json['originalAccessUrl'] = object_raw[self.config.OPARL_MIRROR_PREFIX + ':originalAccessUrl']
            if self.config.OPARL_MIRROR_PREFIX + ':originalDownloadUrl' in object_raw:
                object_json['originalDownloadUrl'] = object_raw[self.config.OPARL_MIRROR_PREFIX + ':originalDownloadUrl']

        # set all the dbrefs generated before
        object_json.update(dbref_data)

        # delete empty lists and dicts
        for key in list(object_json):
            if (isinstance(object_json[key], list) or isinstance(object_json[key], dict)) and not object_json[key]:
                del object_json[key]

        # Save data
        object_json = { '$set': object_json }
        self.correct_document_values(object_json['$set'])
        self.mongodb_request_count += 1
        start_time = time.time()
        result = self.db_raw[object._object_db_name].find_one_and_update(
            query,
            object_json,
            upsert=True,
            return_document=ReturnDocument.AFTER
        )
        self.datalog.debug('%s %s from Body %s saved successfully.' % (object.__name__, result['_id'], self.body_uid))
        self.mongodb_request_time += time.time() - start_time

        # Cache Original ID -> MongoDB ID
        if self.config.USE_MIRROR:
            if object_instance.mirrorId not in self.cache[object.__name__]:
                self.cache[object.__name__][object_instance.mirrorId] = ObjectId(result['_id'])
        else:
            if object_instance.originalId not in self.cache[object.__name__]:
                self.cache[object.__name__][object_instance.originalId] = ObjectId(result['_id'])

        # We need to download files if necessary
        if object == File and not self.config.USE_MIRROR:
            download_file = True
            if self.body_config['force_full_sync'] == 1 and self.last_update and object_instance.modified:
                if object_instance.modified < self.last_update:
                    download_file = False
            if 'downloaded' not in result:
                download_file = True
            elif not result['downloaded']:
                download_file = True
            if 'originalAccessUrl' in object_json['$set'] and download_file:
                file_name_internal = str(result['_id'])
                start_time = time.time()
                file_status = self.download_file(object_json['$set']['originalAccessUrl'], file_name_internal)
                self.file_download_time += time.time() - start_time
                if not file_status:
                    self.datalog.warn('No valid file could be downloaded at File %s from Body %s' % (result['_id'], self.body_uid))
                else:
                    start_time = time.time()
                    object_json_update = {}
                    mime_type = None
                    if 'mimeType' in object_json['$set']:
                        mime_type = object_json['$set']['mimeType']
                    file_name = None
                    if 'fileName' in object_json['$set']:
                        file_name = object_json['$set']['fileName']
                    else:
                        splitted_file_name = object_json['$set']['originalAccessUrl'].split('/')
                        if len(splitted_file_name):
                            if len(splitted_file_name[-1]) > 3 and '.' in splitted_file_name[-1]:
                                file_name = splitted_file_name[-1]
                    if not file_name or not mime_type:
                        self.datalog.warn('No file name or no mime type avaliable at File %s from Body %s' % (
                        result['_id'], self.body_uid))
                    else:
                        content_type = object_json['$set']['mimeType']
                        metadata = {
                            'Content-Disposition': 'filename=%s' % file_name
                        }
                        try:
                            self.s3.fput_object(
                                self.config.S3_BUCKET,
                                "files/%s/%s" % (self.body_uid, file_name_internal),
                                os.path.join(self.config.TMP_FILE_DIR, file_name_internal),
                                content_type=content_type,
                                metadata=metadata
                            )
                            self.datalog.debug('Binary file at File %s from Body %s saved successfully.' % (result['_id'], self.body_uid))
                            object_json_update['downloaded'] = True
                        except (ResponseError, SignatureDoesNotMatch) as err:
                            self.datalog.warn(
                                'Critical error saving file from File %s from Body %s' % (result['_id'], self.body_uid))
                    self.minio_time += time.time() - start_time
                    if 'size' not in object_json['$set']:
                        object_json_update['size'] = os.path.getsize(os.path.join(self.config.TMP_FILE_DIR, file_name_internal))
                    if 'sha1Checksum' not in object_json['$set'] or 'sha512Checksum' not in object_json['$set']:
                        with open(os.path.join(self.config.TMP_FILE_DIR, file_name_internal), 'rb') as checksum_file:
                            checksum_file_content = checksum_file.read()
                            if 'sha1Checksum' not in object_json['$set']:
                                object_json_update['sha1Checksum'] = hashlib.sha1(checksum_file_content).hexdigest()
                            if 'sha512Checksum' not in object_json['$set']:
                                object_json_update['sha512Checksum'] = hashlib.sha512(checksum_file_content).hexdigest()
                    if len(object_json_update.keys()):
                        result = self.db_raw[object._object_db_name].find_one_and_update(
                            query,
                            { '$set': object_json_update },
                            upsert=True,
                            return_document=ReturnDocument.AFTER
                        )
                        self.mongodb_request_count += 1
                    os.remove(os.path.join(self.config.TMP_FILE_DIR, file_name_internal))  # also get all derivativeFile
            else:
                self.download_not_required += 1

        # If we have a Paper with a Location, we need to mark this as official=ris relation if processing is enabled
        if object == Paper and 'location' in object_json['$set'] and self.config.ENABLE_PROCESSING and False:
            for location_obj_id in object_json['$set']['location']:
                if not LocationOrigin.objects(paper=ObjectId(result['_id']), location=location_obj_id, origin='ris').no_cache().count():
                    location_origin = LocationOrigin()
                    location_origin.location = location_obj_id
                    location_origin.paper = ObjectId(result['_id'])
                    location_origin.origin = 'ris'

                    location_origin.save()
                    self.mongodb_request_count += 3
                    paper = Paper.objects(id=ObjectId(result['_id'])).no_cache().first()
                    if location_origin.id not in paper.locationOrigin:
                        paper.locationOrigin.append(location_origin.id)
                        paper.save()
                        self.mongodb_request_count += 1
                    # delete refs
                    location_origin = None
                    paper = None

        return result

    def save_document_values(self, document, key, value):
        if type(document._fields[key]).__name__ == 'DateTimeField':
            try:
                dt = dateutil_parse(value)
            except ValueError:
                delattr(document, key)
                return
            if dt.tzname():
                setattr(document, key, dt.astimezone(pytz.timezone('UTC')).replace(tzinfo=None))
            else:
                setattr(document, key, dt)
        elif key == 'id':
            # temporary fix for missing body/1/
            if '/body/1' not in value:
                value = value.replace('/oparl/v1', '/oparl/v1/body/1')
            setattr(document, 'originalId', value)
        elif key == 'accessUrl':
            setattr(document, 'originalAccessUrl', value)
        elif key == 'downloadUrl':
            setattr(document, 'originalDownloadUrl', value)
        else:
            setattr(document, key, value)

    def correct_document_values(self, document_json):
        for key, value in document_json.items():
            if type(value) == type({}):
                if '$date' in value:
                    document_json[key] = datetime.datetime.fromtimestamp(value['$date'] / 1000).isoformat()

    def get_url_json(self, url, is_list=False, wait=True):
        if url:
            if wait:
                if 'wait_time' in self.body_config:
                    self.wait_time += self.body_config['wait_time']
                    time.sleep(self.body_config['wait_time'])
                else:
                    self.wait_time += self.config.GET_URL_WAIT_TIME
                    time.sleep(self.config.GET_URL_WAIT_TIME)
            self.datalog.info('%s: get %s' % (self.body_config['id'], url))
            self.http_request_count += 1
            start_time = time.time()
            r = requests.get(url, timeout=300)
            self.http_request_time += time.time() - start_time
            if r.status_code == 500:
                self.send_mail(
                    self.config.ADMINS,
                    'critical error at oparl-mirror',
                    'url %s throws an http error 500' % url
                )
                return None
            elif r.status_code == 200:
                try:
                    if not is_list:
                        return r.json()
                    else:
                        list_data = r.json()
                        if 'data' in list_data and 'links' in list_data:
                            return list_data
                except json.decoder.JSONDecodeError:
                    return None
        return None

    def download_file(self, url, file_name):
        try:
            r = requests.get(url, stream=True, timeout=300)
        except (SSLError, ConnectionResetError):
            return False
        if r.status_code != 200:
            return False
        try:
            with open(os.path.join(self.config.TMP_FILE_DIR, file_name), 'wb') as f:
                try:
                    for chunk in r.iter_content(chunk_size=1024):
                        if chunk:
                            f.write(chunk)
                except ChunkedEncodingError:
                    return True
                return True
        except ConnectionResetError:
            return False
