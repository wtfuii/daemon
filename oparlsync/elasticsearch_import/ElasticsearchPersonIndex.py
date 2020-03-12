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

import json
from datetime import datetime
from ..models import *
from ..base_task import BaseTask
from mongoengine.base.datastructures import BaseList
from .ElasticsearchImportBase import ElasticsearchImportBase


class ElasticsearchPersonIndex:

    def person_index(self):
        self.datalog.info('Starting person indexing...')

        if not self.es.indices.exists_alias(name='person-latest'):
            now = datetime.utcnow()
            index_name = 'person-' + now.strftime('%Y%m%d-%H%M')

            mapping = self.es_mapping_generator(Person, 'deref_person')
            mapping['properties']['region'] = {
                'type': 'text'
            }

            self.es.indices.create(index=index_name, body={
                'settings': self.es_settings(),
                'mappings': mapping
            })

            self.es.indices.update_aliases({
                'actions': {
                    'add': {
                        'index': index_name,
                        'alias': 'person-latest'
                    }
                }
            })
        else:
            index_name = list(self.es.indices.get_alias('person-latest'))[0]

        regions = []
        region = self.body.region
        while (region):
            regions.append(str(region.id))
            region = region.parent

        last_index_timestamp = Option.get('last_index_person')

        query_args = {'body': self.body}
        if last_index_timestamp:
            query_args['modified__gt'] = last_index_timestamp

        for person in Person.objects(**query_args).no_cache():
            if person.deleted:
                self.es.delete(
                    index=index_name,
                    id=str(person.id),
                    ignore=[400, 404]
                )
                continue
            person_dict = person.to_dict(deref='deref_person', format_datetime=True, delete='delete_person', clean_none=True)
            person_dict['body_name'] = person.body.name
            person_dict['region'] = regions
            person_dict['legacy'] = 'legacy' in person_dict

            new_doc = self.es.index(
                index=index_name,
                id=str(person.id),
                body=person_dict
            )
            if new_doc['result'] in ['created', 'updated']:
                self.statistics[new_doc['result']] += 1
            else:
                self.datalog.warn('Unknown result at %s' % person.id)
        self.datalog.info('ElasticSearch person import successfull: %s created, %s updated' % (
            self.statistics['created'],
            self.statistics['updated']
        ))
        Option.set('last_index_person', datetime.utcnow(), 'datetime')


