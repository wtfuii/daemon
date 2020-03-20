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

import os
import math
import subprocess
from ..models import *
from ..base_task import BaseTask


class GenerateSitemap(BaseTask):
    name = 'GenerateSitemap'
    services = [
        'mongodb'
    ]

    def __init__(self,  **kwargs):
        super().__init__()
        self.run()

    def run(self):
        if not self.config.ENABLE_PROCESSING:
            return
        bodies = Body.objects().no_cache()

        for body in bodies:
            sitemaps = []
            self.tidy_up(body)
            self.generate_paper_sitemap(body, sitemaps)
            self.generate_meeting_sitemap(body, sitemaps)
            self.generate_file_sitemap(body, sitemaps)
            #self.generate_meta_sitemap(body, sitemaps)
            # Create meta-sitemap

    def tidy_up(self, body):
        for sitemap_file in os.listdir(self.config.SITEMAP_DIR):
            if sitemap_file[0:24] == str(body.id):
                file_path = os.path.join(self.config.SITEMAP_DIR, sitemap_file)
                os.unlink(file_path)

    def generate_paper_sitemap(self, body, sitemaps):
        document_count = Paper.objects(body=body.id).count()
        for sitemap_number in range(0, int(math.ceil(document_count / 50000))):
            papers = Paper.objects(body=body.id, deleted__ne=True)[sitemap_number * 50000:((sitemap_number + 1) * 50000) - 1]
            sitemap_path = os.path.join(self.config.SITEMAP_DIR, '%s-paper-%s.xml' % (body.id, sitemap_number))
            with open(sitemap_path, 'w') as f:
                f.write("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n")
                f.write("<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n")
                for paper in papers.all():
                    if not paper.modified:
                        continue
                    f.write("  <url><loc>%s/paper/%s</loc><lastmod>%s</lastmod></url>\n" % (self.config.SITEMAP_BASE_URL, paper.id, paper.modified.strftime('%Y-%m-%d')))
                f.write("</urlset>\n")
            subprocess.call(['gzip', sitemap_path])
            sitemaps.append('%s-paper-%s.xml' % (body.id, sitemap_number))
            sitemap_number += 1


    def generate_file_sitemap(self, body, sitemaps):
        document_count = File.objects(body=body.id).count()
        for sitemap_number in range(0, int(math.ceil(document_count / 50000))):
            files = File.objects(body=body.id, deleted__ne=True)[sitemap_number * 50000:((sitemap_number + 1) * 50000) - 1]
            sitemap_path = os.path.join(self.config.SITEMAP_DIR, '%s-file-%s.xml' % (body.id, sitemap_number))
            with open(sitemap_path, 'w') as f:
                f.write("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n")
                f.write("<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n")
                for file in files.all():
                    if not file.modified:
                        continue
                    f.write("  <url><loc>%s/file/%s</loc><lastmod>%s</lastmod></url>\n" % (self.config.SITEMAP_BASE_URL, file.id, file.modified.strftime('%Y-%m-%d')))
                f.write("</urlset>\n")
            subprocess.call(['gzip', sitemap_path])
            sitemaps.append('%s-file-%s.xml' % (body.id, sitemap_number))
            sitemap_number += 1

    def generate_meeting_sitemap(self, body, sitemaps):
        document_count = Meeting.objects(body=body.id).count()
        for sitemap_number in range(0, int(math.ceil(document_count / 50000))):
            meetings = Meeting.objects(body=body.id, deleted__ne=True)[sitemap_number * 50000:((sitemap_number + 1) * 50000) - 1]
            sitemap_path = os.path.join(self.config.SITEMAP_DIR, '%s-meeting-%s.xml' % (body.id, sitemap_number))
            with open(sitemap_path, 'w') as f:
                f.write("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n")
                f.write("<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n")
                for meeting in meetings.all():
                    if not meeting.modified:
                        continue
                    f.write("  <url><loc>%s/meeting/%s</loc><lastmod>%s</lastmod></url>\n" % (self.config.SITEMAP_BASE_URL, meeting.id, meeting.modified.strftime('%Y-%m-%d')))
                f.write("</urlset>\n")
            subprocess.call(['gzip', sitemap_path])
            sitemaps.append('%s-meeting-%s.xml' % (body.id, sitemap_number))
            sitemap_number += 1


    def generate_meta_sitemap(self, body, sitemaps):
        meta_sitemap_path = os.path.join(self.config.SITEMAP_DIR, '%s.xml' % body.id)
        with open(meta_sitemap_path, 'w') as f:
            f.write("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n")
            f.write("<sitemapindex xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n")
            for sitemap_name in sitemaps:
                f.write("  <sitemap><loc>%s/static/sitemap/%s.gz</loc></sitemap>\n" % (self.config.SITEMAP_BASE_URL, sitemap_name))
            f.write("</sitemapindex>\n")


