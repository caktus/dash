from __future__ import unicode_literals

import json
import pytz
import redis
import urllib

from dash.api import API
from dash.categories.models import Category, CategoryImage
from dash.dashblocks.models import DashBlockType, DashBlock, DashBlockImage
from dash.dashblocks.templatetags.dashblocks import load_qbs
from dash.orgs.middleware import SetOrgMiddleware
from dash.orgs.models import Org, OrgBackground, Invitation
from dash.orgs.templatetags.dashorgs import display_time, national_phone
from dash.orgs.context_processors import GroupPermWrapper
from dash.stories.models import Story, StoryImage
from datetime import datetime
from django.conf import settings
from django.contrib.auth.models import User, Group
from django.core import mail
from django.core.exceptions import DisallowedHost
from django.core.urlresolvers import reverse, ResolverMatch
from django.db.utils import IntegrityError
from django.http import HttpRequest
from django.utils import timezone
from mock import patch, Mock
from smartmin.tests import SmartminTest
from temba import TembaClient


class UserTest(SmartminTest):
    def setUp(self):
        self.superuser = User.objects.create_superuser(username="super", email="super@user.com", password="super")

        self.admin = self.create_user("Administrator")

    def test_user_profile(self):
        profile_url = reverse('users.user_profile', args=[self.admin.pk])

        response = self.client.get(profile_url)
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(profile_url)
        self.assertEquals(response.status_code, 200)
        self.assertTrue('form' in response.context)

        post_data = dict(username='denzel@nyaruka.com', first_name='Denzel', last_name='Washington',
                         email='washington@nyaruka.com', old_password='Administrator',
                         new_password='Washington2', confirm_new_password='Washington2')

        response = self.client.post(profile_url, post_data, follow=True)

        new_admin = User.objects.get(pk=self.admin.pk)
        self.assertEquals(new_admin.username, 'washington@nyaruka.com')
        self.assertEquals(new_admin.email, 'washington@nyaruka.com')
        self.assertFalse(User.objects.filter(username='denzel@nyaruka.com'))


class DashTest(SmartminTest):

    def setUp(self):
        self.superuser = User.objects.create_superuser(username="super", email="super@user.com", password="super")

        self.admin = self.create_user("Administrator")

        # Clear DashBlockType from old migrations
        DashBlockType.objects.all().delete()

    def clear_cache(self):
        # hardcoded to localhost
        r = redis.StrictRedis(host='localhost', db=1)
        r.flushdb()

    def clear_uploads(self):
        import os
        for org_bg in OrgBackground.objects.all():
            os.remove(org_bg.image.path)

        for cat_image in CategoryImage.objects.all():
            os.remove(cat_image.image.path)

        for story_image in StoryImage.objects.all():
            os.remove(story_image.image.path)

        for dash_image in DashBlockImage.objects.all():
            os.remove(dash_image.image.path)

    def create_org(self, subdomain, user):

        email = subdomain + "@user.com"
        first_name = subdomain + "_First"
        last_name = subdomain + "_Last"
        name = subdomain

        orgs = Org.objects.filter(subdomain=subdomain)
        if orgs:
            org =orgs[0]
            org.name = name
            org.save()
        else:
            org = Org.objects.create(subdomain=subdomain, name=name, language='en', created_by=user, modified_by=user)

        org.administrators.add(user)

        self.assertEquals(Org.objects.filter(subdomain=subdomain).count(), 1)
        return Org.objects.get(subdomain=subdomain)

    def read_json(self, filename):
        from django.conf import settings
        handle = open('%s/test_api/%s.json' % (settings.TESTFILES_DIR, filename))
        contents = handle.read()
        handle.close()
        return contents


class SetOrgMiddlewareTest(DashTest):

    def setUp(self):
        super(SetOrgMiddlewareTest, self).setUp()

        self.middleware = SetOrgMiddleware()

    def mock_view(self, request):
        return MockResponse(204)

    def simulate_process(self, host, url_name):
        """
        Simulates the application of org middleware
        """
        self.request = Mock(spec=HttpRequest)
        self.request.get_host.return_value = host
        self.request.user = self.admin
        self.request.path = '/'
        self.request.META = dict(HTTP_HOST=None)

        response = self.middleware.process_request(self.request)
        if response:
            return response

        self.request.resolver_match = ResolverMatch(self.mock_view, [], {}, url_name)

        return self.middleware.process_view(self.request, self.mock_view, [], {})

    def test_process(self):
        # check white-listed URL with no orgs
        response = self.simulate_process('ureport.io', 'orgs.org_create')
        self.assertIsNone(response)
        self.assertIsNone(self.request.org)
        self.assertIsNone(self.request.user.get_org())

        # check non-white-listed URL with no orgs
        response = self.simulate_process('ureport.io', 'dash.test_test')
        self.assertEquals(response.status_code, 302)
        self.assertEquals(response.url, reverse(settings.SITE_CHOOSER_VIEW_NAME))
        self.assertIsNone(self.request.org)
        self.assertIsNone(self.request.user.get_org())

        # create some orgs..
        ug_org = self.create_org('uganda', self.admin)
        rw_org = self.create_org('rwanda', self.admin)

        # now orgs should be listed in choose page
        response = self.simulate_process('ureport.io', 'dash.test_test')
        self.assertEquals(response.status_code, 302)
        self.assertEquals(response.url, reverse(settings.SITE_CHOOSER_VIEW_NAME))
        self.assertIsNone(self.request.org)
        self.assertIsNone(self.request.user.get_org())

        # white-listing this URL name prevents choose response
        with self.settings(SITE_ALLOW_NO_ORG=('dash.test_test',)):
            response = self.simulate_process('ureport.io', 'dash.test_test')
            self.assertIsNone(response)
            self.assertIsNone(self.request.org)
            self.assertIsNone(self.request.user.get_org())

        # check requests to valid host names based on the Ug org's subdomain
        for host in ('uganda.ureport.io', 'www.UGANDA.ureport.io', 'uganda.staging.ureport.io', 'uganda.localhost'):

            # check white-listed URL
            response = self.simulate_process(host, 'orgs.org_create')
            self.assertIsNone(response)
            self.assertEqual(self.request.org, ug_org)
            self.assertEqual(self.request.user.get_org(), ug_org)

            # check non-white-listed URL
            response = self.simulate_process(host, 'dash.test_test')
            self.assertIsNone(response)
            self.assertEqual(self.request.org, ug_org)
            self.assertEqual(self.request.user.get_org(), ug_org)

        # test invalid subdomain
        response = self.simulate_process('blabla.ureport.io', 'dash.test_test')
        self.assertIsNone(self.request.org)
        self.assertIsNone(self.request.user.get_org())
        self.assertEquals(response.status_code, 302)
        self.assertEquals(response.url, reverse(settings.SITE_CHOOSER_VIEW_NAME))

        # test disallowed host exception
        self.request.get_host.side_effect = DisallowedHost

        response = self.simulate_process('xxx.ureport.io', 'dash.test_test')
        self.assertIsNone(self.request.org)
        self.assertIsNone(self.request.user.get_org())
        self.assertEquals(response.status_code, 302)
        self.assertEquals(response.url, reverse(settings.SITE_CHOOSER_VIEW_NAME))

        rw_org.is_active = False
        rw_org.save()

        response = self.simulate_process('rwanda.ureport.io', 'dash.test_test')
        self.assertIsNone(self.request.org)
        self.assertIsNone(self.request.user.get_org())
        self.assertEquals(response.status_code, 302)
        self.assertEquals(response.url, reverse(settings.SITE_CHOOSER_VIEW_NAME))

        with self.settings(SITE_CHOOSER_VIEW_NAME='dash.test_chooser'):
            response = self.simulate_process('ureport.io', 'dash.test_chooser')
            self.assertIsNone(response)
            self.assertIsNone(self.request.org)
            self.assertIsNone(self.request.user.get_org())

class OrgContextProcessorTestcase(DashTest):
    def test_group_perms_wrapper(self):
        administrators = Group.objects.get(name="Administrators")
        editors = Group.objects.get(name="Editors")
        viewers = Group.objects.get(name="Viewers")

        administrators_wrapper = GroupPermWrapper(administrators)
        self.assertTrue(administrators_wrapper['orgs']['org_edit'])
        self.assertTrue(administrators_wrapper["orgs"]["org_home"])

        editors_wrapper = GroupPermWrapper(editors)
        self.assertFalse(editors_wrapper["orgs"]["org_edit"])
        self.assertTrue(editors_wrapper["orgs"]["org_home"])


class OrgTest(DashTest):

    def setUp(self):
        super(OrgTest, self).setUp()

        self.org = self.create_org("uganda", self.admin)

    def test_org_model(self):
        user = self.create_user("User")

        self.assertEquals(self.org.__unicode__(), 'uganda')

        self.assertIsNone(Org.get_org(None))
        self.assertEquals(Org.get_org(self.admin), self.org)
        self.assertIsNone(Org.get_org(user))

        new_user = Org.create_user('email@example.com', 'secretpassword')
        self.assertIsInstance(new_user, User)
        self.assertEquals(new_user.email, "email@example.com")
        self.assertEquals(new_user.username, "email@example.com")
        self.assertTrue(new_user.check_password("secretpassword"))

        client = self.org.get_temba_client()
        self.assertIsInstance(client, TembaClient)
        self.assertEqual(client.root_url, 'http://localhost:8001/api/v1')
        self.assertEqual(client.token, self.org.api_token)

        with self.settings(SITE_API_HOST='rapidpro.io'):
            client = self.org.get_temba_client()
            self.assertIsInstance(client, TembaClient)
            self.assertEqual(client.root_url, 'https://rapidpro.io/api/v1')
            self.assertEqual(client.token, self.org.api_token)

        api = self.org.get_api()
        self.assertIsInstance(api, API)
        self.assertEquals(api.org, self.org)

        self.assertEquals(self.org.get_user(), self.admin)

        viewer = self.create_user('Viewer')
        editor = self.create_user('Editor')
        self.org.viewers.add(viewer)
        self.org.editors.add(editor)

        self.assertTrue(self.org.get_user_org_group(self.admin))
        self.assertEquals(self.org.get_user_org_group(self.admin).name, "Administrators")
        self.assertTrue(self.org.get_user_org_group(editor))
        self.assertEquals(self.org.get_user_org_group(editor).name, "Editors")
        self.assertTrue(self.org.get_user_org_group(viewer))
        self.assertEquals(self.org.get_user_org_group(viewer).name, "Viewers")
        self.assertIsNone(self.org.get_user_org_group(user))

        org_users = self.org.get_org_users()
        self.assertEquals(len(org_users), 3)
        self.assertIn(self.admin, org_users)
        self.assertIn(editor, org_users)
        self.assertIn(viewer, org_users)

        org_admins = self.org.get_org_admins()
        self.assertEquals(len(org_admins), 1)
        self.assertIn(self.admin, org_admins)

        org_editors = self.org.get_org_editors()
        self.assertEquals(len(org_editors), 1)
        self.assertIn(editor, org_editors)

        org_viewers = self.org.get_org_viewers()
        self.assertEquals(len(org_viewers), 1)
        self.assertIn(viewer, org_viewers)

        self.assertIsNone(self.org.get_config('field_name'))
        self.org.set_config('field_name', 'field_value')
        self.assertEquals(self.org.get_config('field_name'), 'field_value')

        self.org.set_config('other_field_name', 'other_value')
        self.assertEquals(self.org.get_config('field_name'), 'field_value')
        self.assertEquals(self.org.get_config('other_field_name'), 'other_value')

        self.org._config = None
        self.assertEquals(self.org.get_config('field_name'), 'field_value')
        self.assertEquals(self.org.get_config('other_field_name'), 'other_value')

    def test_get_most_active_regions(self):
        self.org.set_config('gender_label', 'Gender')

        with patch('dash.api.API.get_contact_field_results') as mock:
            mock.return_value = [dict(label='LABEL_1', set=15, unset=5),
                                 dict(label='LABEL_2', set=100, unset=200),
                                 dict(label='LABEL_3', set=50, unset=30)]

            self.assertEquals(self.org.get_most_active_regions(), ['LABEL_2', 'LABEL_3', 'LABEL_1'])
            mock.assert_called_once_with('Gender', dict(location='State'))

        with patch('dash.api.API.get_contact_field_results') as mock:
            self.clear_cache()
            mock.return_value = None

            self.assertEquals(self.org.get_most_active_regions(), [])
            mock.assert_called_once_with('Gender', dict(location='State'))

    def test_organize_categories_data(self):

        self.org.set_config('born_label', "Born")
        self.org.set_config('registration_label', "Registration")
        self.org.set_config('occupation_label', "Occupation")

        self.assertEquals(self.org.organize_categories_data('random_field', []), [])
        self.assertEquals(self.org.organize_categories_data('born', []), [])
        self.assertEquals(self.org.organize_categories_data('registration', []), [])
        self.assertEquals(self.org.organize_categories_data('occupation', []), [])
        self.assertEquals(self.org.organize_categories_data('random_field', ['random_api_data']), ['random_api_data'])

        tz = pytz.timezone('Africa/Kigali')
        with patch.object(timezone, 'now', return_value=tz.localize(datetime(2014, 9, 26, 10, 20, 30, 40))):

            self.assertEquals(self.org.organize_categories_data('born', [dict(categories=[])]), [dict(categories=[])])
            self.assertEquals(self.org.organize_categories_data('born', [dict(categories=[dict(label='123', count=50)])]), [dict(categories=[])])
            self.assertEquals(self.org.organize_categories_data('born', [dict(categories=[dict(label='12345', count=50)])]), [dict(categories=[])])
            self.assertEquals(self.org.organize_categories_data('born', [dict(categories=[dict(label='abcd', count=50)])]), [dict(categories=[])])
            self.assertEquals(self.org.organize_categories_data('born', [dict(categories=[dict(label='1899', count=50)])]), [dict(categories=[])])

            self.assertEquals(self.org.organize_categories_data('born', [dict(categories=[dict(label='2010', count=50)])]),
                              [dict(categories=[dict(label='0-10', count=50)])])

            self.assertEquals(self.org.organize_categories_data('born', [dict(categories=[dict(label='2000', count=50)])]),
                              [dict(categories=[dict(label='10-20', count=50)])])


            born_api_data = [dict(categories=[dict(label='1700', count=10),
                                              dict(label='1998', count=10),
                                              dict(label='123', count=10),
                                              dict(label='abcd', count=1),
                                              dict(label='2005', count=50),
                                              dict(label='97675', count=10),
                                              dict(label='1990', count=20),
                                              dict(label='1995', count=5),
                                              dict(label='2009', count=30),
                                              dict(label='2001', count=10),
                                              dict(label='2011', count=25)])]

            expected_born_data = [dict(categories=[dict(label='0-10', count=105),
                                                   dict(label='10-20', count=25),
                                                   dict(label='20-30', count=20)])]

            self.assertEquals(self.org.organize_categories_data('born', born_api_data), expected_born_data)

            self.assertEquals(self.org.organize_categories_data('registration', [dict(categories=[])]), [dict(categories=[{'count': 0, 'label': '03/24/14'}, {'count': 0, 'label': '03/31/14'}, {'count': 0, 'label': '04/07/14'}, {'count': 0, 'label': '04/14/14'}, {'count': 0, 'label': '04/21/14'}, {'count': 0, 'label': '04/28/14'}, {'count': 0, 'label': '05/05/14'}, {'count': 0, 'label': '05/12/14'}, {'count': 0, 'label': '05/19/14'}, {'count': 0, 'label': '05/26/14'}, {'count': 0, 'label': '06/02/14'}, {'count': 0, 'label': '06/09/14'}, {'count': 0, 'label': '06/16/14'}, {'count': 0, 'label': '06/23/14'}, {'count': 0, 'label': '06/30/14'}, {'count': 0, 'label': '07/07/14'}, {'count': 0, 'label': '07/14/14'}, {'count': 0, 'label': '07/21/14'}, {'count': 0, 'label': '07/28/14'}, {'count': 0, 'label': '08/04/14'}, {'count': 0, 'label': '08/11/14'}, {'count': 0, 'label': '08/18/14'}, {'count': 0, 'label': '08/25/14'}, {'count': 0, 'label': '09/01/14'}, {'count': 0, 'label': '09/08/14'}, {'count': 0, 'label': '09/15/14'}, {'count': 0, 'label': '09/22/14'}])])
            self.assertEquals(self.org.organize_categories_data('registration', [dict(categories=[dict(label='26-9-2013 21:30', count=20)])]), [dict(categories=[{'count': 0, 'label': '03/24/14'}, {'count': 0, 'label': '03/31/14'}, {'count': 0, 'label': '04/07/14'}, {'count': 0, 'label': '04/14/14'}, {'count': 0, 'label': '04/21/14'}, {'count': 0, 'label': '04/28/14'}, {'count': 0, 'label': '05/05/14'}, {'count': 0, 'label': '05/12/14'}, {'count': 0, 'label': '05/19/14'}, {'count': 0, 'label': '05/26/14'}, {'count': 0, 'label': '06/02/14'}, {'count': 0, 'label': '06/09/14'}, {'count': 0, 'label': '06/16/14'}, {'count': 0, 'label': '06/23/14'}, {'count': 0, 'label': '06/30/14'}, {'count': 0, 'label': '07/07/14'}, {'count': 0, 'label': '07/14/14'}, {'count': 0, 'label': '07/21/14'}, {'count': 0, 'label': '07/28/14'}, {'count': 0, 'label': '08/04/14'}, {'count': 0, 'label': '08/11/14'}, {'count': 0, 'label': '08/18/14'}, {'count': 0, 'label': '08/25/14'}, {'count': 0, 'label': '09/01/14'}, {'count': 0, 'label': '09/08/14'}, {'count': 0, 'label': '09/15/14'}, {'count': 0, 'label': '09/22/14'}])])
            self.assertEquals(self.org.organize_categories_data('registration', [dict(categories=[dict(label='31-3-2014 21:30', count=20)])]),
                              [dict(categories=[{'count': 0, 'label': '03/24/14'}, {'count': 20, 'label': '03/31/14'}, {'count': 0, 'label': '04/07/14'}, {'count': 0, 'label': '04/14/14'}, {'count': 0, 'label': '04/21/14'}, {'count': 0, 'label': '04/28/14'}, {'count': 0, 'label': '05/05/14'}, {'count': 0, 'label': '05/12/14'}, {'count': 0, 'label': '05/19/14'}, {'count': 0, 'label': '05/26/14'}, {'count': 0, 'label': '06/02/14'}, {'count': 0, 'label': '06/09/14'}, {'count': 0, 'label': '06/16/14'}, {'count': 0, 'label': '06/23/14'}, {'count': 0, 'label': '06/30/14'}, {'count': 0, 'label': '07/07/14'}, {'count': 0, 'label': '07/14/14'}, {'count': 0, 'label': '07/21/14'}, {'count': 0, 'label': '07/28/14'}, {'count': 0, 'label': '08/04/14'}, {'count': 0, 'label': '08/11/14'}, {'count': 0, 'label': '08/18/14'}, {'count': 0, 'label': '08/25/14'}, {'count': 0, 'label': '09/01/14'}, {'count': 0, 'label': '09/08/14'}, {'count': 0, 'label': '09/15/14'}, {'count': 0, 'label': '09/22/14'}])])

            self.assertEquals(self.org.organize_categories_data('registration', [dict(categories=[dict(label='31-3-2014 21:30', count=20),
                                                                                                  dict(label='3-4-2014 20:54',  count=15)])]),
                              [dict(categories=[{'count': 0, 'label': '03/24/14'}, {'count': 35, 'label': '03/31/14'}, {'count': 0, 'label': '04/07/14'}, {'count': 0, 'label': '04/14/14'}, {'count': 0, 'label': '04/21/14'}, {'count': 0, 'label': '04/28/14'}, {'count': 0, 'label': '05/05/14'}, {'count': 0, 'label': '05/12/14'}, {'count': 0, 'label': '05/19/14'}, {'count': 0, 'label': '05/26/14'}, {'count': 0, 'label': '06/02/14'}, {'count': 0, 'label': '06/09/14'}, {'count': 0, 'label': '06/16/14'}, {'count': 0, 'label': '06/23/14'}, {'count': 0, 'label': '06/30/14'}, {'count': 0, 'label': '07/07/14'}, {'count': 0, 'label': '07/14/14'}, {'count': 0, 'label': '07/21/14'}, {'count': 0, 'label': '07/28/14'}, {'count': 0, 'label': '08/04/14'}, {'count': 0, 'label': '08/11/14'}, {'count': 0, 'label': '08/18/14'}, {'count': 0, 'label': '08/25/14'}, {'count': 0, 'label': '09/01/14'}, {'count': 0, 'label': '09/08/14'}, {'count': 0, 'label': '09/15/14'}, {'count': 0, 'label': '09/22/14'}])])

            self.assertEquals(self.org.organize_categories_data('registration', [dict(categories=[dict(label='31-3-2014 21:30', count=20),
                                                                                                  dict(label='3-4-2014 20:54',  count=15),
                                                                                                  dict(label='8-4-2014 18:43', count=10)])]),
                              [dict(categories=[{'count': 0, 'label': '03/24/14'}, {'count': 35, 'label': '03/31/14'}, {'count': 10, 'label': '04/07/14'}, {'count': 0, 'label': '04/14/14'}, {'count': 0, 'label': '04/21/14'}, {'count': 0, 'label': '04/28/14'}, {'count': 0, 'label': '05/05/14'}, {'count': 0, 'label': '05/12/14'}, {'count': 0, 'label': '05/19/14'}, {'count': 0, 'label': '05/26/14'}, {'count': 0, 'label': '06/02/14'}, {'count': 0, 'label': '06/09/14'}, {'count': 0, 'label': '06/16/14'}, {'count': 0, 'label': '06/23/14'}, {'count': 0, 'label': '06/30/14'}, {'count': 0, 'label': '07/07/14'}, {'count': 0, 'label': '07/14/14'}, {'count': 0, 'label': '07/21/14'}, {'count': 0, 'label': '07/28/14'}, {'count': 0, 'label': '08/04/14'}, {'count': 0, 'label': '08/11/14'}, {'count': 0, 'label': '08/18/14'}, {'count': 0, 'label': '08/25/14'}, {'count': 0, 'label': '09/01/14'}, {'count': 0, 'label': '09/08/14'}, {'count': 0, 'label': '09/15/14'}, {'count': 0, 'label': '09/22/14'}])])

            self.assertEquals(self.org.organize_categories_data('registration', [dict(categories=[dict(label='31-3-2014 21:30', count=20),
                                                                                                  dict(label='3-4-2014 20:54',  count=15),
                                                                                                  dict(label='8-4-2014 18:43', count=10),
                                                                                                  dict(label='10-10-2014 12:54', count=100)])]),
                              [dict(categories=[{'count': 0, 'label': '03/24/14'}, {'count': 35, 'label': '03/31/14'}, {'count': 10, 'label': '04/07/14'}, {'count': 0, 'label': '04/14/14'}, {'count': 0, 'label': '04/21/14'}, {'count': 0, 'label': '04/28/14'}, {'count': 0, 'label': '05/05/14'}, {'count': 0, 'label': '05/12/14'}, {'count': 0, 'label': '05/19/14'}, {'count': 0, 'label': '05/26/14'}, {'count': 0, 'label': '06/02/14'}, {'count': 0, 'label': '06/09/14'}, {'count': 0, 'label': '06/16/14'}, {'count': 0, 'label': '06/23/14'}, {'count': 0, 'label': '06/30/14'}, {'count': 0, 'label': '07/07/14'}, {'count': 0, 'label': '07/14/14'}, {'count': 0, 'label': '07/21/14'}, {'count': 0, 'label': '07/28/14'}, {'count': 0, 'label': '08/04/14'}, {'count': 0, 'label': '08/11/14'}, {'count': 0, 'label': '08/18/14'}, {'count': 0, 'label': '08/25/14'}, {'count': 0, 'label': '09/01/14'}, {'count': 0, 'label': '09/08/14'}, {'count': 0, 'label': '09/15/14'}, {'count': 0, 'label': '09/22/14'}])])


            # support parsing of label from datetime fields
            self.assertEquals(self.org.organize_categories_data('registration', [dict(categories=[dict(label='2014-03-31T21:30:00Z', count=20),
                                                                                                  dict(label='2014-04-03T20:54:00Z', count=15),
                                                                                                  dict(label='2014-04-08T18:43:00Z', count=10),
                                                                                                  dict(label='2014-10-10T12:54:00Z', count=100)])]),
                              [dict(categories=[{'count': 0, 'label': '03/24/14'}, {'count': 35, 'label': '03/31/14'}, {'count': 10, 'label': '04/07/14'}, {'count': 0, 'label': '04/14/14'}, {'count': 0, 'label': '04/21/14'}, {'count': 0, 'label': '04/28/14'}, {'count': 0, 'label': '05/05/14'}, {'count': 0, 'label': '05/12/14'}, {'count': 0, 'label': '05/19/14'}, {'count': 0, 'label': '05/26/14'}, {'count': 0, 'label': '06/02/14'}, {'count': 0, 'label': '06/09/14'}, {'count': 0, 'label': '06/16/14'}, {'count': 0, 'label': '06/23/14'}, {'count': 0, 'label': '06/30/14'}, {'count': 0, 'label': '07/07/14'}, {'count': 0, 'label': '07/14/14'}, {'count': 0, 'label': '07/21/14'}, {'count': 0, 'label': '07/28/14'}, {'count': 0, 'label': '08/04/14'}, {'count': 0, 'label': '08/11/14'}, {'count': 0, 'label': '08/18/14'}, {'count': 0, 'label': '08/25/14'}, {'count': 0, 'label': '09/01/14'}, {'count': 0, 'label': '09/08/14'}, {'count': 0, 'label': '09/15/14'}, {'count': 0, 'label': '09/22/14'}])])

            self.assertEquals(self.org.organize_categories_data('occupation', [dict(categories=[])]), [dict(categories=[])])
            self.assertEquals(self.org.organize_categories_data('occupation', [dict(categories=[dict(label='All Responses', count=20)])]), [dict(categories=[])])
            self.assertEquals(self.org.organize_categories_data('occupation', [dict(categories=[dict(label='All Responses', count=20),
                                                                                                dict(label='Student', count=50)])]), [dict(categories=[dict(label='Student', count=50)])])

            self.assertEquals(self.org.organize_categories_data('occupation', [dict(categories=[dict(label='Student', count=500),
                                                                                                dict(label='Player', count=300),
                                                                                                dict(label='Journalist', count=50),
                                                                                                dict(label='Actor', count=30),
                                                                                                dict(label='Manager', count=150),
                                                                                                dict(label='All Responses', count=20),
                                                                                                dict(label='Teacher', count=10),
                                                                                                dict(label='Officer', count=8),
                                                                                                dict(label='Nurse', count=5),
                                                                                                dict(label='Cameraman', count=5),
                                                                                                dict(label='Writer', count=3),
                                                                                                dict(label='Photographer', count=2),
                                                                                                dict(label='DJ', count=1),
                                                                                                dict(label='Mechanic', count=1),
                                                                                                dict(label='Engineer', count=1),
                                                                                                dict(label='Professor', count=1)])]),


                              [dict(categories=[dict(label='Student', count=500),
                                                dict(label='Player', count=300),
                                                dict(label='Journalist', count=50),
                                                dict(label='Actor', count=30),
                                                dict(label='Manager', count=150),
                                                dict(label='Teacher', count=10),
                                                dict(label='Officer', count=8),
                                                dict(label='Nurse', count=5),
                                                dict(label='Cameraman', count=5)
                                               ])])

    def test_org_create(self):
        create_url = reverse("orgs.org_create")

        response = self.client.get(create_url)
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(create_url)
        self.assertLoginRedirect(response)

        self.login(self.superuser)
        response = self.client.get(create_url)
        self.assertEquals(200, response.status_code)
        self.assertFalse(Org.objects.filter(name="kLab"))
        self.assertEquals(User.objects.all().count(), 4)

        user_alice = User.objects.create_user("alicefox")

        data = dict(name="kLab", subdomain="klab", timezone="Africa/Kigali", administrators=[user_alice.pk])
        response = self.client.post(create_url, data, follow=True)
        self.assertTrue('form' not in response.context)
        self.assertTrue(Org.objects.filter(name="kLab"))
        org = Org.objects.get(name="kLab")
        self.assertEquals(User.objects.all().count(), 5)
        self.assertTrue(org.administrators.filter(username="alicefox"))
        self.assertEquals(org.timezone, "Africa/Kigali")

    def test_org_update(self):
        update_url = reverse("orgs.org_update", args=[self.org.pk])

        response = self.client.get(update_url)
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(update_url)
        self.assertLoginRedirect(response)

        self.login(self.superuser)
        response = self.client.get(update_url)
        self.assertEquals(200, response.status_code)
        self.assertFalse(Org.objects.filter(name="Burundi"))
        self.assertEquals(len(response.context['form'].fields), 10)

        post_data = dict(name="Burundi", timezone="Africa/Bujumbura", subdomain="burundi", is_active=True, male_label="male", female_label='female', administrators=self.admin.pk)
        response = self.client.post(update_url, post_data)
        self.assertEquals(response.status_code, 302)

        response = self.client.post(update_url, post_data, follow=True)
        self.assertEquals(response.status_code, 200)
        org = Org.objects.get(pk=self.org.pk)
        self.assertEquals(org.name, "Burundi")
        self.assertEquals(org.subdomain, "burundi")
        self.assertEquals(org.timezone, "Africa/Bujumbura")
        self.assertEquals(response.request['PATH_INFO'], reverse('orgs.org_list'))

    def test_org_list(self):
        list_url = reverse("orgs.org_list")

        response = self.client.get(list_url)
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(list_url)
        self.assertLoginRedirect(response)

        self.login(self.superuser)
        response = self.client.get(list_url)
        self.assertEquals(response.status_code, 200)
        self.assertTrue(response.context['object_list'])
        self.assertTrue(self.org in response.context['object_list'])
        self.assertEquals(len(response.context['fields']), 4)

    def test_org_choose(self):
        choose_url = reverse('orgs.org_choose')

        Org.objects.all().delete()

        response = self.client.get(choose_url)
        self.assertLoginRedirect(response)

        self.login(self.superuser)
        response = self.client.get(choose_url)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(choose_url, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse('orgs.org_list'))

        self.login(self.admin)
        response = self.client.get(choose_url)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(choose_url, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse('users.user_login'))

        self.org = self.create_org("uganda", self.admin)

        # with a subdomain
        response = self.client.get(choose_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 302)
        response = self.client.get(choose_url, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.request['PATH_INFO'], reverse('orgs.org_home'))

        # without subdomain
        response = self.client.get(choose_url)
        self.assertEquals(response.status_code, 200)
        self.assertEquals(response.request['PATH_INFO'], reverse('orgs.org_choose'))
        self.assertEquals(len(response.context['orgs']), 1)
        self.assertTrue(self.org in response.context['orgs'])
        self.assertFalse('org' in response.context)
        self.assertTrue('form' in response.context)
        self.assertEquals(len(response.context['form'].fields), 2)
        self.assertTrue('organization' in response.context['form'].fields)
        self.assertTrue('loc' in response.context['form'].fields)

        org_choices = response.context['form'].fields['organization'].choices.queryset
        self.assertEquals(len(org_choices), 1)
        self.assertTrue(self.org in org_choices)

        post_data = dict(organization=self.org.pk)
        response = self.client.post(choose_url, post_data, follow=True)
        self.assertTrue('org' in response.context)
        self.assertEquals(self.org, response.context['org'])
        self.assertEquals(response.request['PATH_INFO'], reverse('orgs.org_home'))

        user = self.create_user('user')
        other_org = self.create_org('other', user)

        post_data = dict(organization=other_org.pk)
        response = self.client.post(choose_url, post_data, follow=True)
        self.assertFalse('org' in response.context)
        self.assertTrue('form' in response.context)
        self.assertTrue(response.context['form'].errors)
        self.assertEquals(response.request['PATH_INFO'], reverse('orgs.org_choose'))

        self.nigeria = self.create_org('nigeria', self.admin)

        response = self.client.get(choose_url)
        self.assertEquals(response.status_code, 200)
        self.assertEquals(response.request['PATH_INFO'], reverse('orgs.org_choose'))
        self.assertEquals(len(response.context['orgs']), 2)
        self.assertTrue(self.org in response.context['orgs'])
        self.assertTrue(self.nigeria in response.context['orgs'])
        self.assertFalse(other_org in response.context['orgs'])
        self.assertFalse('org' in response.context)
        self.assertTrue('form' in response.context)
        self.assertEquals(len(response.context['form'].fields), 2)
        self.assertTrue('organization' in response.context['form'].fields)
        self.assertTrue('loc' in response.context['form'].fields)

        org_choices = response.context['form'].fields['organization'].choices.queryset
        self.assertEquals(len(org_choices), 2)
        self.assertTrue(self.org in org_choices)
        self.assertTrue(self.nigeria in org_choices)

        post_data = dict(organization=self.nigeria.pk)
        response = self.client.post(choose_url, post_data, follow=True)
        self.assertTrue('org' in response.context)
        self.assertEquals(self.nigeria, response.context['org'])
        self.assertEquals(response.request['PATH_INFO'], reverse('orgs.org_home'))

        # test overriding the user home page
        with self.settings(SITE_USER_HOME='/example/home'):
            response = self.client.post(choose_url, post_data, follow=True)
            self.assertEquals(response.request['PATH_INFO'], '/example/home')

    def test_org_home(self):
        home_url = reverse('orgs.org_home')

        response = self.client.get(home_url)
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(home_url, SERVER_NAME="uganda.ureport.io")
        self.assertEquals(200, response.status_code)
        self.assertEquals(response.context['object'], self.org)
        self.assertEquals(response.context['org'], self.org)
        self.assertTrue('Not Set' in response.content)

        self.org.api_token = '0' * 64
        self.org.save()

        self.login(self.admin)
        response = self.client.get(home_url, SERVER_NAME="uganda.ureport.io")
        self.assertEquals(200, response.status_code)
        self.assertEquals(response.context['object'], self.org)
        self.assertEquals(response.context['org'], self.org)
        self.assertFalse('Not Set' in response.content)
        self.assertTrue('*' * 32 in response.content)

    def test_org_edit(self):

        with patch('dash.orgs.models.API') as mock:
            mock.return_value.get_country_geojson.return_value = dict(type="FeatureCollection",
                                                                      features=[dict(type='Feature',
                                                                                     properties=dict(id="R3713501",
                                                                                                     level=1,
                                                                                                     name="Abia"),
                                                                                     geometry=dict(type="MultiPolygon",
                                                                                                   coordinates=[[[[7, 5]]]]
                                                                                                   )
                                                                                     )
                                                                      ])



            edit_url = reverse("orgs.org_edit")

            self.login(self.admin)
            self.admin.set_org(self.org)

            response = self.client.get(edit_url, SERVER_NAME="uganda.ureport.io")
            self.assertEquals(response.status_code, 200)
            self.assertTrue(response.context['form'])
            self.assertEquals(len(response.context['form'].fields), 11)

            # featured state is currently disabled; adjust the following lines
            self.assertTrue('featured_state' not in response.context['form'].fields) # this make sure the featured state are disabled
            # self.assertEquals(len(response.context['form'].fields['featured_state'].choices), 1)
            # self.assertEquals(response.context['form'].fields['featured_state'].choices[0][0], 'R3713501')
            # self.assertEquals(response.context['form'].fields['featured_state'].choices[0][1], 'Abia')


            self.assertEquals(response.context['form'].initial['name'], 'uganda')
            self.assertEquals(response.context['object'], self.org)
            self.assertEquals(response.context['object'], response.context['org'])
            self.assertEquals(response.context['object'].subdomain, 'uganda')

            post_data = dict()
            response = self.client.post(edit_url, post_data, SERVER_NAME="uganda.ureport.io")
            self.assertTrue(response.context['form'])

            errors = response.context['form'].errors
            self.assertEquals(len(errors.keys()), 2)
            self.assertTrue('name' in errors)
            self.assertTrue('shortcode' in errors)
            self.assertEquals(errors['name'][0], 'This field is required.')
            self.assertEquals(errors['shortcode'][0], 'This field is required.')

            post_data = dict(name="Rwanda",
                             shortcode="224433",
                             featured_state="R3713501")

            response = self.client.post(edit_url, post_data, SERVER_NAME="uganda.ureport.io")
            self.assertEquals(response.status_code, 302)

            response = self.client.post(edit_url, post_data, follow=True, SERVER_NAME="uganda.ureport.io")
            self.assertFalse('form' in response.context)
            org = Org.objects.get(pk=self.org.pk)
            self.assertEquals(org.name, "Rwanda")
            self.assertEquals(org.get_config('shortcode'), "224433")


            # featured state is currenty disabled, adjust the following lines
            self.assertFalse(org.get_config('featured_state')) # this make sure the featured state are disabled
            #self.assertEquals(org.get_config('featured_state'), "R3713501")

            self.assertEquals(response.request['PATH_INFO'], reverse('orgs.org_home'))

            response = self.client.get(edit_url, SERVER_NAME="uganda.ureport.io")
            self.assertEquals(response.status_code, 200)
            form = response.context['form']
            self.assertEquals(form.initial['shortcode'], "224433")
            self.assertEquals(form.initial['name'], "Rwanda")

    def test_org_chooser(self):
        chooser_url = reverse('orgs.org_chooser')

        response = self.client.get(chooser_url)
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['orgs']), 1)
        self.assertTrue(self.org in response.context['orgs'])
        self.assertEquals(response.context['orgs'][0].host, "http://uganda.localhost:8000")

        self.org2 = self.create_org('nigeria', self.admin)

        response = self.client.get(chooser_url)
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['orgs']), 2)
        self.assertTrue(self.org in response.context['orgs'])
        self.assertTrue(self.org2 in response.context['orgs'])

        self.org2.is_active = False
        self.org2.save()

        response = self.client.get(chooser_url)
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['orgs']), 1)
        self.assertTrue(self.org in response.context['orgs'])
        self.assertFalse(self.org2 in response.context['orgs'])

    def test_invitation_model(self):
        invitation = Invitation.objects.create(org=self.org,
                                               user_group="V",
                                               email="norkans7@gmail.com",
                                               created_by=self.admin,
                                               modified_by=self.admin)

        with patch('dash.orgs.models.Invitation.generate_random_string') as mock:
            mock.side_effect = [invitation.secret, 'A' * 64]

            second_invitation = Invitation.objects.create(org=self.org,
                                                          user_group="E",
                                                          email="eric@gmail.com",
                                                          created_by=self.admin,
                                                          modified_by=self.admin)

            self.assertEquals(second_invitation.secret, 'A' * 64)

            invitation.email = None
            self.assertIsNone(invitation.send_email())

    def test_manage_accounts(self):
        manage_accounts_url = reverse('orgs.org_manage_accounts')
        self.editor = self.create_user("Editor")
        self.user = self.create_user("User")

        self.org = self.create_org("uganda", self.admin)

        self.login(self.admin)
        self.admin.set_org(self.org)

        self.org.editors.add(self.editor)
        self.org.administrators.add(self.user)

        response = self.client.get(manage_accounts_url, SERVER_NAME="uganda.ureport.io")

        self.assertEquals(200, response.status_code)

        # we have 12 fields in the form including 9 checkboxes for the three users, an emails field a user group field and 'loc' field.
        self.assertEquals(9, len(response.context['form'].fields))
        self.assertTrue('emails' in response.context['form'].fields)
        self.assertTrue('user_group' in response.context['form'].fields)
        for user in [self.editor, self.user, self.admin]:
            self.assertTrue("administrators_%d" % user.pk in response.context['form'].fields)
            self.assertTrue("editors_%d" % user.pk in response.context['form'].fields)

        self.assertFalse(response.context['form'].fields['emails'].initial)
        self.assertEquals('E', response.context['form'].fields['user_group'].initial)

        post_data = dict()

        # keep all the admins
        post_data['administrators_%d' % self.admin.pk] = 'on'
        post_data['administrators_%d' % self.user.pk] = 'on'
        post_data['administrators_%d' % self.editor.pk] = 'on'

        # add self.editor to editors
        post_data['editors_%d' % self.editor.pk] = 'on'
        post_data['user_group'] = 'E'

        response = self.client.post(manage_accounts_url, post_data, SERVER_NAME="uganda.ureport.io")
        self.assertEquals(302, response.status_code)

        org = Org.objects.get(pk=self.org.pk)
        self.assertEquals(org.administrators.all().count(), 3)
        self.assertFalse(org.viewers.all())
        self.assertTrue(org.editors.all())
        self.assertEquals(org.editors.all()[0].pk, self.editor.pk)

        # add to post_data an email to invite as admin
        post_data['emails'] = "norkans7gmail.com"
        post_data['user_group'] = 'A'
        response = self.client.post(manage_accounts_url, post_data, SERVER_NAME="uganda.ureport.io")
        self.assertTrue('emails' in response.context['form'].errors)
        self.assertEquals("One of the emails you entered is invalid.", response.context['form'].errors['emails'][0])

        # now post with right email
        post_data['emails'] = "norkans7@gmail.com"
        post_data['user_group'] = 'A'
        response = self.client.post(manage_accounts_url, post_data, SERVER_NAME="uganda.ureport.io")

        # an invitation is created and sent by email
        self.assertEquals(1, Invitation.objects.all().count())
        self.assertTrue(len(mail.outbox) == 1)

        invitation = Invitation.objects.get()

        self.assertEquals(invitation.org, self.org)
        self.assertEquals(invitation.email, "norkans7@gmail.com")
        self.assertEquals(invitation.user_group, "A")

        # pretend our invite was acted on
        Invitation.objects.all().update(is_active=False)

        # send another invitation, different group
        post_data['emails'] = "norkans7@gmail.com"
        post_data['user_group'] = 'E'
        self.client.post(manage_accounts_url, post_data, SERVER_NAME="uganda.ureport.io")

        # old invite should be updated
        new_invite = Invitation.objects.all().first()
        self.assertEquals(1, Invitation.objects.all().count())
        self.assertEquals(invitation.pk, new_invite.pk)
        self.assertEquals('E', new_invite.user_group)
        self.assertEquals(2, len(mail.outbox))
        self.assertTrue(new_invite.is_active)

        # post many emails to the form
        post_data['emails'] = "norbert@nyaruka.com,code@nyaruka.com"
        post_data['user_group'] = 'A'
        self.client.post(manage_accounts_url, post_data, SERVER_NAME="uganda.ureport.io")

        # now 2 new invitations are created and sent
        self.assertEquals(3, Invitation.objects.all().count())
        self.assertEquals(4, len(mail.outbox))

    def test_join(self):
        editor_invitation = Invitation.objects.create(org=self.org,
                                               user_group="E",
                                               email="norkans7@gmail.com",
                                               created_by=self.admin,
                                               modified_by=self.admin)

        self.org2 = self.create_org("kenya", self.admin)
        editor_join_url = reverse('orgs.org_join', args=[editor_invitation.secret])
        self.client.logout()

        # if no user is logged we redirect to the create_login page
        response = self.client.get(editor_join_url, SERVER_NAME="uganda.ureport.io")
        self.assertEquals(302, response.status_code)
        response = self.client.get(editor_join_url, follow=True, SERVER_NAME="uganda.ureport.io")
        self.assertEquals(response.request['PATH_INFO'], reverse('orgs.org_create_login', args=[editor_invitation.secret]))

        # a user is already logged in
        self.invited_editor = self.create_user("InvitedEditor")
        self.login(self.invited_editor)

        with patch('dash.orgs.views.OrgCRUDL.Join.get_object') as mock:
            mock.return_value = None

            response = self.client.get(editor_join_url, follow=True, SERVER_NAME="kenya.ureport.io")
            self.assertEquals(response.request['PATH_INFO'], '/')

        response = self.client.get(editor_join_url, SERVER_NAME="kenya.ureport.io")
        self.assertEquals(302, response.status_code)

        response = self.client.get(editor_join_url, follow=True, SERVER_NAME="kenya.ureport.io")
        self.assertEquals(200, response.status_code)
        self.assertEquals(response.wsgi_request.org, self.org)
        self.assertEquals(response.request['PATH_INFO'], editor_join_url)

        response = self.client.get(editor_join_url, SERVER_NAME="uganda.ureport.io")
        self.assertEquals(200, response.status_code)

        self.assertEquals(self.org.pk, response.context['org'].pk)
        # we have a form without field except one 'loc'
        self.assertEquals(1, len(response.context['form'].fields))

        post_data = dict()
        response = self.client.post(editor_join_url, post_data, follow=True, SERVER_NAME="uganda.ureport.io")
        self.assertEquals(200, response.status_code)

        self.assertTrue(self.invited_editor in self.org.editors.all())
        self.assertFalse(Invitation.objects.get(pk=editor_invitation.pk).is_active)

        # test overriding the user home page
        with self.settings(SITE_USER_HOME='/example/home'):
            invitation = Invitation.objects.create(org=self.org, user_group="E", email="norkans7@gmail.com",
                                                   created_by=self.admin, modified_by=self.admin)
            join_url = reverse('orgs.org_join', args=[invitation.secret])
            post_data = dict()
            response = self.client.post(join_url, post_data, follow=True, SERVER_NAME="uganda.ureport.io")
            self.assertEquals(response.request['PATH_INFO'], '/example/home')

    def test_create_login(self):
        admin_invitation = Invitation.objects.create(org=self.org,
                                                     user_group="A",
                                                     email="norkans7@gmail.com",
                                                     created_by=self.admin,
                                                     modified_by=self.admin)

        self.org2 = self.create_org("kenya", self.admin)

        admin_create_login_url = reverse('orgs.org_create_login', args=[admin_invitation.secret])
        self.client.logout()

        with patch('dash.orgs.views.OrgCRUDL.CreateLogin.get_object') as mock:
            mock.return_value = None

            response = self.client.get(admin_create_login_url, follow=True, SERVER_NAME="kenya.ureport.io")
            self.assertEquals(response.request['PATH_INFO'], '/')

        response = self.client.get(admin_create_login_url, SERVER_NAME="kenya.ureport.io")
        self.assertEquals(302, response.status_code)

        response = self.client.get(admin_create_login_url, follow=True, SERVER_NAME="kenya.ureport.io")
        self.assertEquals(200, response.status_code)
        self.assertEquals(response.wsgi_request.org, self.org)
        self.assertEquals(response.request['PATH_INFO'], admin_create_login_url)

        response = self.client.get(admin_create_login_url, SERVER_NAME="uganda.ureport.io")
        self.assertEquals(200, response.status_code)

        self.assertEquals(self.org.pk, response.context['org'].pk)

        # we have a form with 4 fields and one hidden 'loc'
        self.assertEquals(5, len(response.context['form'].fields))
        self.assertTrue('first_name' in response.context['form'].fields)
        self.assertTrue('last_name' in response.context['form'].fields)
        self.assertTrue('email' in response.context['form'].fields)
        self.assertTrue('password' in response.context['form'].fields)

        post_data = dict()
        post_data['first_name'] = "Norbert"
        post_data['last_name'] = "Kwizera"
        post_data['email'] = "norkans7@gmail.com"
        post_data['password'] = "norbert"

        response = self.client.post(admin_create_login_url, post_data, SERVER_NAME="uganda.ureport.io")
        self.assertEquals(200, response.status_code)
        self.assertTrue('form' in response.context)
        self.assertTrue(response.context['form'].errors)
        self.assertFalse(User.objects.filter(email='norkans7@gmail.com'))
        self.assertTrue(Invitation.objects.get(pk=admin_invitation.pk).is_active)

        post_data = dict()
        post_data['first_name'] = "Norbert"
        post_data['last_name'] = "Kwizera"
        post_data['email'] = "norkans7@gmail.com"
        post_data['password'] = "norbertkwizeranorbert"

        response = self.client.post(admin_create_login_url, post_data, follow=True, SERVER_NAME="uganda.ureport.io")
        self.assertEquals(200, response.status_code)

        new_invited_user = User.objects.get(email="norkans7@gmail.com")
        self.assertTrue(new_invited_user in self.org.administrators.all())
        self.assertFalse(Invitation.objects.get(pk=admin_invitation.pk).is_active)

        viewer_invitation = Invitation.objects.create(org=self.org,
                                                      user_group="V",
                                                      email="norkans7@gmail.com",
                                                      created_by=self.admin,
                                                      modified_by=self.admin)
        viewer_create_login_url = reverse('orgs.org_create_login', args=[viewer_invitation.secret])

        post_data = dict()
        post_data['first_name'] = "Norbert"
        post_data['last_name'] = "Kwizera"
        post_data['email'] = "norkans7@gmail.com"
        post_data['password'] = "norbertkwizeranorbert"

        response = self.client.post(viewer_create_login_url, post_data, SERVER_NAME="uganda.ureport.io")
        self.assertEquals(200, response.status_code)
        self.assertTrue('form' in response.context)
        self.assertTrue(response.context['form'].errors)
        self.assertTrue(User.objects.filter(email='norkans7@gmail.com'))
        self.assertFalse(Invitation.objects.get(pk=admin_invitation.pk).is_active)
        self.assertTrue(Invitation.objects.get(pk=viewer_invitation.pk).is_active)

    def test_dashorgs_templatetags(self):
        self.assertEquals(display_time("2014-11-04T15:11:34Z", self.org), "Nov 04, 2014 15:11")

        self.org.set_timezone('Africa/Kigali')
        self.org.save()
        self.assertEquals(display_time("2014-11-04T15:11:34Z", self.org), "Nov 04, 2014 17:11")

        self.assertEquals(display_time("2014-11-04T15:11:34Z", self.org, '%A, %B %d, %Y'), "Tuesday, November 04, 2014")

        self.assertEquals(national_phone('+250788505050'), "0788 505 050")
        self.assertEquals(national_phone('250788505050'), "250788505050")
        self.assertEquals(national_phone('+93700325998'), "070 032 5998")

class OrgBackgroundTest(DashTest):

    def setUp(self):
        super(OrgBackgroundTest, self).setUp()

        self.uganda = self.create_org('uganda', self.admin)
        self.nigeria = self.create_org('nigeria', self.admin)

    def test_org_background(self):
        create_url = reverse('orgs.orgbackground_create')

        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['form'].fields), 4)
        self.assertTrue('org' not in response.context['form'].fields)

        upload = open("%s/image.jpg" % settings.TESTFILES_DIR, "r")

        post_data = dict(name="Orange Pattern", background_type="P", image=upload)
        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        uganda_org_bg = OrgBackground.objects.order_by('-pk')[0]
        self.assertEquals(uganda_org_bg.org, self.uganda)
        self.assertEquals(uganda_org_bg.name, 'Orange Pattern')

        response = self.client.get(create_url, SERVER_NAME='nigeria.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['form'].fields), 4)
        self.assertTrue('org' not in response.context['form'].fields)

        upload = open("%s/image.jpg" % settings.TESTFILES_DIR, "r")

        post_data = dict(name="Orange Pattern", background_type="P", image=upload)
        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='nigeria.ureport.io')
        self.assertEquals(response.status_code, 200)
        nigeria_org_bg = OrgBackground.objects.order_by('-pk')[0]
        self.assertEquals(nigeria_org_bg.org, self.nigeria)
        self.assertEquals(nigeria_org_bg.name, 'Orange Pattern')

        list_url = reverse('orgs.orgbackground_list')

        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(len(response.context['object_list']), 1)
        self.assertEquals(response.context['object_list'][0], uganda_org_bg)

        response = self.client.get(list_url, SERVER_NAME='nigeria.ureport.io')
        self.assertEquals(len(response.context['object_list']), 1)
        self.assertEquals(response.context['object_list'][0], nigeria_org_bg)

        uganda_bg_update_url = reverse('orgs.orgbackground_update', args=[uganda_org_bg.pk])
        nigeria_bg_update_url = reverse('orgs.orgbackground_update', args=[nigeria_org_bg.pk])

        response = self.client.get(uganda_bg_update_url, SERVER_NAME='nigeria.ureport.io')
        self.assertLoginRedirect(response)

        response = self.client.get(nigeria_bg_update_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        response = self.client.get(uganda_bg_update_url, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.request['PATH_INFO'], uganda_bg_update_url)
        self.assertEquals(len(response.context['form'].fields), 5)
        self.assertTrue('org' not in response.context['form'].fields)

        upload = open("%s/image.jpg" % settings.TESTFILES_DIR, "r")
        post_data = dict(name="Orange Pattern Updated", background_type="P", image=upload)
        response = self.client.post(uganda_bg_update_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.request['PATH_INFO'], list_url)
        self.assertEquals(len(response.context['object_list']), 1)
        self.assertEquals(response.context['object_list'][0].name, "Orange Pattern Updated")

        self.login(self.superuser)
        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['form'].fields), 5)
        self.assertTrue('org' in response.context['form'].fields)

        upload = open("%s/image.jpg" % settings.TESTFILES_DIR, "r")

        post_data = dict(name="Blue Pattern", background_type="P", image=upload)
        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertTrue('form' in response.context)
        self.assertTrue('org' in response.context['form'].errors)

        upload = open("%s/image.jpg" % settings.TESTFILES_DIR, "r")

        post_data = dict(name="Blue Pattern", background_type="P", image=upload, org=self.uganda.pk)

        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertTrue('form' not in response.context)
        blue_bg = OrgBackground.objects.get(name="Blue Pattern")
        self.assertEquals(blue_bg.org, self.uganda)

        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(len(response.context['object_list']), OrgBackground.objects.count())

        response = self.client.get(nigeria_bg_update_url, SERVER_NAME='nigeria.ureport.io')
        self.assertEquals(response.request['PATH_INFO'], nigeria_bg_update_url)
        self.assertEquals(len(response.context['form'].fields), 5)

        self.clear_uploads()

class MockResponse(object):

    def __init__(self, status_code, content=''):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code != 200:
            raise Exception("Server returned %s" % str(self.status_code))

    def json(self, **kwargs):
        return json.loads(self.content)

class APITest(DashTest):
    def setUp(self):
        super(APITest, self).setUp()

        self.org = self.create_org("uganda", self.admin)
        self.org.api_token = 'UGANDA_API_TOKEN'
        self.org.set_config('state_label', 'LGA')
        self.org.set_config('district_label', 'Province')

        self.api = API(self.org)

        # clear our cache
        self.clear_cache()

    @patch('requests.models.Response', MockResponse)
    def test_get_group(self):
        with patch('requests.get') as mock_request_get:
            self.clear_cache()
            mock_request_get.return_value = MockResponse(200, json.dumps(dict(results=["GROUP_DICT"])))

            self.assertEquals(self.api.get_group('group_name'), "GROUP_DICT")
            mock_request_get.assert_called_once_with('%s/api/v1/groups.json' % settings.API_ENDPOINT,
                                                     params={'name': 'group_name'},
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})


        with patch('requests.get') as mock_request_get:
            self.clear_cache()
            mock_request_get.return_value = MockResponse(200, json.dumps(dict(results=[])))

            self.assertIsNone(self.api.get_group('group_name'))
            mock_request_get.assert_called_once_with('%s/api/v1/groups.json' % settings.API_ENDPOINT,
                                                     params={'name': 'group_name'},
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})


        with patch('requests.get') as mock_request_get:
            self.clear_cache()
            mock_request_get.return_value = MockResponse(200, json.dumps(dict(no_results_key="")))

            self.assertIsNone(self.api.get_group('group_name'))
            mock_request_get.assert_called_once_with('%s/api/v1/groups.json' % settings.API_ENDPOINT,
                                                     params={'name': 'group_name'},
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

        with patch('requests.get') as mock_request_get:
            self.clear_cache()
            mock_request_get.return_value = MockResponse(404, json.dumps(dict(error="Not Found")))

            self.assertIsNone(self.api.get_group('group_name'))
            mock_request_get.assert_called_once_with('%s/api/v1/groups.json' % settings.API_ENDPOINT,
                                                     params={'name': 'group_name'},
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

        with patch('requests.get') as mock_request_get:
            self.clear_cache()
            mock_request_get.return_value = MockResponse(200, 'invalid_json')

            self.assertIsNone(self.api.get_group('group_name'))
            mock_request_get.assert_called_once_with('%s/api/v1/groups.json' % settings.API_ENDPOINT,
                                                     params={'name': 'group_name'},
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

    @patch('requests.models.Response', MockResponse)
    def test_get_ruleset_results(self):
        with patch('requests.get') as mock_request_get:
            mock_request_get.return_value = MockResponse(200, json.dumps(dict(no_results_key="")))

            self.assertIsNone(self.api.get_ruleset_results(101))
            mock_request_get.assert_called_once_with('%s/api/v1/results.json?ruleset=101&segment=null' % settings.API_ENDPOINT,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertIsNone(self.api.get_ruleset_results(101, dict(location='State')))
            mock_request_get.assert_called_with('%s/api/v1/results.json?ruleset=101&segment=%s' % (settings.API_ENDPOINT, urllib.quote(unicode(json.dumps(dict(location='LGA'))).encode('utf8'))) ,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertIsNone(self.api.get_ruleset_results(101, dict(location='District')))
            mock_request_get.assert_called_with('%s/api/v1/results.json?ruleset=101&segment=%s' % (settings.API_ENDPOINT, urllib.quote(unicode(json.dumps(dict(location='Province'))).encode('utf8'))) ,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})
            self.assertEquals(mock_request_get.call_count, 3)

        with patch('requests.get') as mock_request_get:
            mock_request_get.return_value = MockResponse(404, json.dumps(dict(error="Not Found")))

            self.assertIsNone(self.api.get_ruleset_results(101))
            mock_request_get.assert_called_once_with('%s/api/v1/results.json?ruleset=101&segment=null' % settings.API_ENDPOINT,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertIsNone(self.api.get_ruleset_results(101, dict(location='State')))
            mock_request_get.assert_called_with('%s/api/v1/results.json?ruleset=101&segment=%s' % (settings.API_ENDPOINT, urllib.quote(unicode(json.dumps(dict(location='LGA'))).encode('utf8'))) ,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertIsNone(self.api.get_ruleset_results(101, dict(location='District')))
            mock_request_get.assert_called_with('%s/api/v1/results.json?ruleset=101&segment=%s' % (settings.API_ENDPOINT, urllib.quote(unicode(json.dumps(dict(location='Province'))).encode('utf8'))) ,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(mock_request_get.call_count, 3)


        with patch('requests.get') as mock_request_get:
            mock_request_get.return_value = MockResponse(200, 'invalid_json')

            self.assertIsNone(self.api.get_ruleset_results(101))
            mock_request_get.assert_called_once_with('%s/api/v1/results.json?ruleset=101&segment=null' % settings.API_ENDPOINT,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertIsNone(self.api.get_ruleset_results(101, dict(location='State')))
            mock_request_get.assert_called_with('%s/api/v1/results.json?ruleset=101&segment=%s' % (settings.API_ENDPOINT, urllib.quote(unicode(json.dumps(dict(location='LGA'))).encode('utf8'))) ,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertIsNone(self.api.get_ruleset_results(101, dict(location='District')))
            mock_request_get.assert_called_with('%s/api/v1/results.json?ruleset=101&segment=%s' % (settings.API_ENDPOINT, urllib.quote(unicode(json.dumps(dict(location='Province'))).encode('utf8'))) ,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(mock_request_get.call_count, 3)

        with patch('requests.get') as mock_request_get:
            mock_request_get.return_value = MockResponse(200, json.dumps(dict(results=["RULESET_DATA"])))

            self.assertEquals(self.api.get_ruleset_results(101), ["RULESET_DATA"])
            mock_request_get.assert_called_once_with('%s/api/v1/results.json?ruleset=101&segment=null' % settings.API_ENDPOINT,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(self.api.get_ruleset_results(101, dict(location='State')), ["RULESET_DATA"])
            mock_request_get.assert_called_with('%s/api/v1/results.json?ruleset=101&segment=%s' % (settings.API_ENDPOINT, urllib.quote(unicode(json.dumps(dict(location='LGA'))).encode('utf8'))) ,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(self.api.get_ruleset_results(101, dict(location='District')), ["RULESET_DATA"])
            mock_request_get.assert_called_with('%s/api/v1/results.json?ruleset=101&segment=%s' % (settings.API_ENDPOINT, urllib.quote(unicode(json.dumps(dict(location='Province'))).encode('utf8'))) ,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(mock_request_get.call_count, 3)

    @patch('requests.models.Response', MockResponse)
    def test_get_contact_field_results(self):
        with patch('requests.get') as mock_request_get:
            mock_request_get.return_value = MockResponse(200, json.dumps(dict(results=["CONTACT_FIELD_DATA"])))

            self.assertEquals(self.api.get_contact_field_results('contact_field_name'), ["CONTACT_FIELD_DATA"])
            mock_request_get.assert_called_once_with('%s/api/v1/results.json?contact_field=contact_field_name&segment=null' % settings.API_ENDPOINT,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(self.api.get_contact_field_results('contact_field_name', dict(location='State')), ["CONTACT_FIELD_DATA"])
            mock_request_get.assert_called_with('%s/api/v1/results.json?contact_field=contact_field_name&segment=%s' % (settings.API_ENDPOINT, urllib.quote(unicode(json.dumps(dict(location='LGA'))).encode('utf8'))),
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(self.api.get_contact_field_results('contact_field_name', dict(location='District')), ["CONTACT_FIELD_DATA"])
            mock_request_get.assert_called_with('%s/api/v1/results.json?contact_field=contact_field_name&segment=%s' % (settings.API_ENDPOINT, urllib.quote(unicode(json.dumps(dict(location='Province'))).encode('utf8'))),
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(mock_request_get.call_count, 3)

        self.clear_cache()

        with patch('requests.get') as mock_request_get:
            mock_request_get.return_value = MockResponse(200, json.dumps(dict(no_results_key=["CONTACT_FIELD_DATA"])))

            self.assertIsNone(self.api.get_contact_field_results('contact_field_name'))
            mock_request_get.assert_called_once_with('%s/api/v1/results.json?contact_field=contact_field_name&segment=null' % settings.API_ENDPOINT,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

        with patch('requests.get') as mock_request_get:
            mock_request_get.return_value = MockResponse(200, 'invalid_json')

            self.assertIsNone(self.api.get_contact_field_results('contact_field_name'))
            mock_request_get.assert_called_once_with('%s/api/v1/results.json?contact_field=contact_field_name&segment=null' % settings.API_ENDPOINT,
                                                     headers={'Content-type': 'application/json',
                                                              'Accept': 'application/json',
                                                              'Authorization': 'Token %s' % self.org.api_token})

    @patch('requests.models.Response', MockResponse)
    def test_get_flows(self):
        with patch('requests.get') as mock_request_get:
            mock_request_get.side_effect = [MockResponse(200,
                                                         self.read_json('flows_page_1')
                                                         ),

                                            MockResponse(200,
                                                         self.read_json('flows_page_2')
                                                         )
                                            ]

            self.assertEquals(self.api.get_flows(), [dict(name="FLOW_1",
                                                          rulesets=["FLOW_1_RULESET_DICT"]),
                                                     dict(name="FLOW_3",
                                                          rulesets=["FLOW_3_RULESET_DICT"]),
                                                     dict(name="FLOW_5",
                                                          rulesets=["FLOW_5_RULESET_DICT"]),
                                                     dict(name="FLOW_6",
                                                          rulesets=["FLOW_6_RULESET_DICT"])
                                                     ])

            mock_request_get.assert_any_call('%s/api/v1/flows.json' % settings.API_ENDPOINT,
                                             headers={'Content-type': 'application/json',
                                                       'Accept': 'application/json',
                                                       'Authorization': 'Token %s' % self.org.api_token})

            mock_request_get.assert_any_call('NEXT_PAGE',
                                             headers={'Content-type': 'application/json',
                                                       'Accept': 'application/json',
                                                       'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(mock_request_get.call_count, 2)

        with patch('requests.get') as mock_request_get:
            self.clear_cache()

            mock_request_get.side_effect = [MockResponse(200,
                                                         self.read_json('flows_missing_next_key')
                                                         ),

                                            MockResponse(200,
                                                         self.read_json('flows_page_2')
                                                         )
                                            ]

            self.assertEquals(self.api.get_flows(), [dict(name="FLOW_1",
                                                          rulesets=["FLOW_1_RULESET_DICT"]),
                                                     dict(name="FLOW_3",
                                                          rulesets=["FLOW_3_RULESET_DICT"])])

            mock_request_get.assert_called_once_with('%s/api/v1/flows.json' % settings.API_ENDPOINT,
                                                      headers={'Content-type': 'application/json',
                                                               'Accept': 'application/json',
                                                               'Authorization': 'Token %s' % self.org.api_token})

        with patch('requests.get') as mock_request_get:
            self.clear_cache()
            mock_request_get.return_value = MockResponse(200, 'invalid_json')

            self.assertIsNone(self.api.get_flows())

            mock_request_get.assert_called_once_with('%s/api/v1/flows.json' % settings.API_ENDPOINT,
                                                      headers={'Content-type': 'application/json',
                                                               'Accept': 'application/json',
                                                               'Authorization': 'Token %s' % self.org.api_token})

    @patch('requests.models.Response', MockResponse)
    def test_get_flow(self):
        with patch('requests.get') as mock_request_get:
            mock_request_get.side_effect = [MockResponse(200,
                                                         self.read_json('flows_page_1')
                                                         ),

                                            MockResponse(200,
                                                         self.read_json('flows_page_2')
                                                         )
                                            ]

            self.assertEquals(self.api.get_flow(5), dict(name="FLOW_1", rulesets=['FLOW_1_RULESET_DICT']))

            mock_request_get.assert_any_call('%s/api/v1/flows.json?flow=5' % settings.API_ENDPOINT,
                                             headers={'Content-type': 'application/json',
                                                       'Accept': 'application/json',
                                                       'Authorization': 'Token %s' % self.org.api_token})

            mock_request_get.assert_any_call('NEXT_PAGE',
                                             headers={'Content-type': 'application/json',
                                                       'Accept': 'application/json',
                                                       'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(mock_request_get.call_count, 2)


        with patch('requests.get') as mock_request_get:
            self.clear_cache()

            mock_request_get.side_effect = [MockResponse(404,
                                                         self.read_json('flows_page_1')
                                                         ),

                                            MockResponse(404,
                                                         self.read_json('flows_page_2')
                                                         )
                                            ]

            self.assertIsNone(self.api.get_flow(5))

            mock_request_get.assert_any_call('%s/api/v1/flows.json?flow=5' % settings.API_ENDPOINT,
                                             headers={'Content-type': 'application/json',
                                                       'Accept': 'application/json',
                                                       'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(mock_request_get.call_count, 1)

        with patch('requests.get') as mock_request_get:
            self.clear_cache()

            mock_request_get.side_effect = [MockResponse(200,
                                                         self.read_json('flows_page_1')
                                                         ),

                                            MockResponse(404,
                                                         self.read_json('flows_page_2')
                                                         )
                                            ]

            self.assertIsNone(self.api.get_flow(5))

            mock_request_get.assert_any_call('%s/api/v1/flows.json?flow=5' % settings.API_ENDPOINT,
                                                      headers={'Content-type': 'application/json',
                                                               'Accept': 'application/json',
                                                               'Authorization': 'Token %s' % self.org.api_token})

            mock_request_get.assert_any_call('NEXT_PAGE',
                                             headers={'Content-type': 'application/json',
                                                       'Accept': 'application/json',
                                                       'Authorization': 'Token %s' % self.org.api_token})

            self.assertEquals(mock_request_get.call_count, 2)

    @patch('requests.models.Response', MockResponse)
    def test_build_boundaries(self):
        with patch('requests.get') as mock_request_get:
            self.clear_cache()
            mock_request_get.side_effect = [MockResponse(200,
                                                         self.read_json('boundaries_page_1')
                                                         ),
                                            MockResponse(200,
                                                         self.read_json('boundaries_page_2')
                                                         )]

            boundary_cached = dict()
            boundary_cached['geojson:%d' % self.org.id] = dict(type='FeatureCollection',
                                                               features=[dict(type='Feature',
                                                                              geometry="B_GEOMETRY_DICT_2",
                                                                              properties=dict(name="B_NAME_2",
                                                                                              id="B_BOUNDARY_2",
                                                                                              level=1)),
                                                                         dict(type='Feature',
                                                                              geometry="B_GEOMETRY_DICT_3",
                                                                              properties=dict(name="B_NAME_3",
                                                                                              id="B_BOUNDARY_3",
                                                                                              level=1))])
            boundary_cached['geojson:%d:B_BOUNDARY_2' % self.org.id] = dict(type='FeatureCollection',
                                                                            features=[dict(type='Feature',
                                                                                           geometry="B_GEOMETRY_DICT_4",
                                                                                           properties=dict(name="B_NAME_4",
                                                                                                           id="B_BOUNDARY_4",
                                                                                                           level=2))])

            self.assertEquals(self.api._build_boundaries(), boundary_cached)


        with patch('requests.get') as mock_request_get:
            self.clear_cache()
            mock_request_get.side_effect = [MockResponse(200,
                                                         self.read_json('boundaries_missing_next_key')
                                                         ),
                                            MockResponse(200,
                                                         self.read_json('boundaries_page_2')
                                                         )]

            boundary_cached = dict()
            boundary_cached['geojson:%d' % self.org.id] = dict(type='FeatureCollection',
                                                               features=[dict(type='Feature',
                                                                              geometry="B_GEOMETRY_DICT_2",
                                                                              properties=dict(name="B_NAME_2",
                                                                                              id="B_BOUNDARY_2",
                                                                                              level=1))
                                                                         ])

            self.assertEquals(self.api._build_boundaries(), boundary_cached)


        with patch('requests.get') as mock_request_get:
            self.clear_cache()
            mock_request_get.side_effect = [MockResponse(200,
                                                         'invalid_json'
                                                         ),
                                            MockResponse(200,
                                                         self.read_json('boundaries_page_2')
                                                         )]

            boundary_cached = dict()
            boundary_cached['geojson:%d' % self.org.id] = dict(type='FeatureCollection',
                                                               features=[])

            self.assertRaises(ValueError, lambda: self.api._build_boundaries())
            mock_request_get.assert_called_once_with('%s/api/v1/boundaries.json' % settings.API_ENDPOINT,
                                                     headers={'Content-type': 'application/json',
                                                     'Accept': 'application/json',
                                                     'Authorization': 'Token %s' % self.org.api_token})

        with patch('requests.get') as mock_request_get:
            self.clear_cache()
            mock_request_get.side_effect = [MockResponse(200,
                                                         self.read_json('boundaries_page_1')
                                                         ),
                                            MockResponse(200,
                                                         self.read_json('boundaries_page_2')
                                                         )]

            boundary_cached = dict()
            boundary_cached['geojson:%d' % self.org.id] = dict(type='FeatureCollection',
                                                               features=[dict(type='Feature',
                                                                              geometry="B_GEOMETRY_DICT_2",
                                                                              properties=dict(name="B_NAME_2",
                                                                                              id="B_BOUNDARY_2",
                                                                                              level=1)),
                                                                         dict(type='Feature',
                                                                              geometry="B_GEOMETRY_DICT_3",
                                                                              properties=dict(name="B_NAME_3",
                                                                                              id="B_BOUNDARY_3",
                                                                                              level=1))])
            boundary_cached['geojson:%d:B_BOUNDARY_2' % self.org.id] = dict(type='FeatureCollection',
                                                                            features=[dict(type='Feature',
                                                                                           geometry="B_GEOMETRY_DICT_4",
                                                                                           properties=dict(name="B_NAME_4",
                                                                                                           id="B_BOUNDARY_4",
                                                                                                           level=2))])

            self.assertEquals(self.api.get_country_geojson(), boundary_cached['geojson:%d' % self.org.id])

        with patch('requests.get') as mock_request_get:
            self.clear_cache()

            mock_request_get.side_effect = [MockResponse(200,
                                                         self.read_json('boundaries_page_1')
                                                         ),
                                            MockResponse(200,
                                                         self.read_json('boundaries_page_2')
                                                         )]

            boundary_cached = dict()
            boundary_cached['geojson:%d' % self.org.id] = dict(type='FeatureCollection',
                                                               features=[dict(type='Feature',
                                                                              geometry="B_GEOMETRY_DICT_2",
                                                                              properties=dict(name="B_NAME_2",
                                                                                              id="B_BOUNDARY_2",
                                                                                              level=1)),
                                                                         dict(type='Feature',
                                                                              geometry="B_GEOMETRY_DICT_3",
                                                                              properties=dict(name="B_NAME_3",
                                                                                              id="B_BOUNDARY_3",
                                                                                              level=1))])
            boundary_cached['geojson:%d:B_BOUNDARY_2' % self.org.id] = dict(type='FeatureCollection',
                                                                            features=[dict(type='Feature',
                                                                                           geometry="B_GEOMETRY_DICT_4",
                                                                                           properties=dict(name="B_NAME_4",
                                                                                                           id="B_BOUNDARY_4",
                                                                                                           level=2))])

            self.assertEquals(self.api.get_state_geojson('B_BOUNDARY_2'), boundary_cached['geojson:%d:B_BOUNDARY_2' % self.org.id])


class CategoryTest(DashTest):

    def setUp(self):
        super(CategoryTest, self).setUp()
        self.uganda = self.create_org('uganda', self.admin)
        self.nigeria = self.create_org('nigeria', self.admin)

    def test_category_model(self):
        category1 = Category.objects.create(name='category 1',
                                            org=self.uganda,
                                            image='categories/image.jpg',
                                            created_by=self.admin,
                                            modified_by=self.admin)

        self.assertEquals(category1.__unicode__(), 'uganda - category 1')

        with self.assertRaises(IntegrityError):
            Category.objects.create(name='category 1',
                                    org=self.uganda,
                                    created_by=self.admin,
                                    modified_by=self.admin)

    def test_category_get_first_image(self):
        category1 = Category.objects.create(name='category 1',
                                            org=self.uganda,
                                            created_by=self.admin,
                                            modified_by=self.admin)

        self.assertIsNone(category1.get_first_image())

        category_image1 = CategoryImage.objects.create(category=category1,
                                                       name='image 1',
                                                       image=None,
                                                       created_by=self.admin,
                                                       modified_by=self.admin)

        self.assertEquals(unicode(category_image1), 'category 1 - image 1')
        self.assertIsNone(category1.get_first_image())

        category_image1.image = 'categories/image.jpg'
        category_image1.is_active = False
        category_image1.save()

        self.assertIsNone(category1.get_first_image())

        category_image1.is_active = True
        category_image1.save()

        self.assertTrue(category1.get_first_image())
        self.assertEquals(category1.get_first_image(), category_image1.image)

    def test_create_category(self):
        create_url = reverse('categories.category_create')

        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['form'].fields), 2)
        self.assertTrue('org' not in response.context['form'].fields)

        post_data = dict()
        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertTrue(response.context['form'].errors)
        self.assertTrue('name' in response.context['form'].errors)

        post_data = dict(name="Health")
        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        category = Category.objects.order_by('-pk')[0]
        self.assertEquals(response.status_code, 200)
        self.assertEquals(response.request['PATH_INFO'], reverse('categories.category_list'))
        self.assertEquals(category.name, "Health")
        self.assertEquals(category.org, self.uganda)

        self.login(self.superuser)
        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['form'].fields), 3)
        self.assertTrue('org' in response.context['form'].fields)

        post_data= dict(name="Education", org=self.uganda.pk)
        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        category = Category.objects.order_by('-pk')[0]
        self.assertEquals(response.status_code, 200)
        self.assertEquals(response.request['PATH_INFO'], reverse('categories.category_list'))
        self.assertEquals(category.name, "Education")
        self.assertEquals(category.org, self.uganda)

    def test_list_category(self):
        uganda_health = Category.objects.create(name="Health", org=self.uganda, created_by=self.admin, modified_by=self.admin)
        uganda_education = Category.objects.create(name="Education", org=self.uganda, created_by=self.admin, modified_by=self.admin)

        nigeria_health = Category.objects.create(name="Health", org=self.nigeria, created_by=self.admin, modified_by=self.admin)

        list_url =reverse('categories.category_list')

        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(len(response.context['object_list']), 2)
        self.assertTrue(nigeria_health not in response.context['object_list'])
        self.assertTrue(uganda_health in response.context['object_list'])
        self.assertTrue(uganda_education in response.context['object_list'])

        response = self.client.get(list_url, SERVER_NAME='nigeria.ureport.io')
        self.assertEquals(len(response.context['object_list']), 1)
        self.assertTrue(uganda_health not in response.context['object_list'])
        self.assertTrue(uganda_education not in response.context['object_list'])
        self.assertTrue(nigeria_health in response.context['object_list'])
        self.assertEquals(len(response.context['fields']), 3)

        self.login(self.superuser)
        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(len(response.context['fields']), 4)
        self.assertEquals(len(response.context['object_list']), 2)
        self.assertTrue(uganda_health in response.context['object_list'])
        self.assertTrue(uganda_education in response.context['object_list'])
        self.assertTrue(nigeria_health not in response.context['object_list'])

    def test_category_update(self):
        uganda_health = Category.objects.create(name="Health", org=self.uganda, created_by=self.admin, modified_by=self.admin)

        nigeria_health = Category.objects.create(name="Health", org=self.nigeria, created_by=self.admin, modified_by=self.admin)

        uganda_update_url = reverse('categories.category_update', args=[uganda_health.pk])
        nigeria_update_url = reverse('categories.category_update', args=[nigeria_health.pk])

        response = self.client.get(uganda_update_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)

        response = self.client.get(uganda_update_url, SERVER_NAME='nigeria.ureport.io')
        self.assertLoginRedirect(response)

        response = self.client.get(nigeria_update_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        response = self.client.get(uganda_update_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['form'].fields), 3)

        post_data = dict(name='Sanitation', is_active=True)
        response = self.client.post(uganda_update_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.request['PATH_INFO'], reverse('categories.category_list'))
        category = Category.objects.get(pk=uganda_health.pk)
        self.assertEquals(category.name, "Sanitation")

    def test_create_category_image(self):
        uganda_health = Category.objects.create(name="Health", org=self.uganda, created_by=self.admin, modified_by=self.admin)

        nigeria_health = Category.objects.create(name="Health", org=self.nigeria, created_by=self.admin, modified_by=self.admin)

        create_url = reverse('categories.categoryimage_create')

        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(create_url, SERVER_NAME='nigeria.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['form'].fields), 4)
        self.assertEquals(response.context['form'].fields['category'].choices.queryset.count(), 1)
        self.assertEquals(nigeria_health, response.context['form'].fields['category'].choices.queryset[0])

        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['form'].fields), 4)
        self.assertEquals(response.context['form'].fields['category'].choices.queryset.count(), 1)
        self.assertEquals(uganda_health, response.context['form'].fields['category'].choices.queryset[0])

        upload = open("%s/image.jpg" % settings.TESTFILES_DIR, "r")
        post_data = dict(name="health hero", image=upload, category=uganda_health.pk)
        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        cat_image = CategoryImage.objects.order_by('-pk')[0]
        self.assertEquals(cat_image.name, 'health hero')
        self.assertEquals(cat_image.category, uganda_health)

        list_url = reverse('categories.categoryimage_list')

        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(len(response.context['object_list']), 1)
        self.assertTrue(cat_image in response.context['object_list'])

        response = self.client.get(list_url, SERVER_NAME='nigeria.ureport.io')
        self.assertEquals(len(response.context['object_list']), 0)
        self.assertTrue(cat_image  not in response.context['object_list'])

        update_url = reverse('categories.categoryimage_update', args=[cat_image.pk])

        response = self.client.get(update_url, SERVER_NAME='nigeria.ureport.io')
        self.assertLoginRedirect(response)

        response = self.client.get(update_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(len(response.context['form'].fields), 5)

        upload = open("%s/image.jpg" % settings.TESTFILES_DIR, "r")
        post_data = dict(name='health image', image=upload, category=uganda_health.pk, is_active=True)
        response = self.client.post(update_url, post_data, follow=True, SERVER_NAME='uganda.ureprt.io')
        self.assertEquals(response.request['PATH_INFO'], reverse('categories.categoryimage_list'))
        cat_image = CategoryImage.objects.filter(pk=cat_image.pk)[0]
        self.assertEquals(cat_image.name, 'health image')

        self.clear_uploads()


class StoryTest(DashTest):
    def setUp(self):
        super(StoryTest, self).setUp()
        self.uganda = self.create_org('uganda', self.admin)
        self.nigeria = self.create_org('nigeria', self.admin)


        self.health_uganda = Category.objects.create(org=self.uganda,
                                                     name="Health",
                                                     created_by=self.admin,
                                                     modified_by=self.admin)

        self.education_nigeria = Category.objects.create(org=self.nigeria,
                                                         name="Education",
                                                         created_by=self.admin,
                                                         modified_by=self.admin)

    def test_story_model(self):
        self.story = Story.objects.create(title="Story 1",
                                          content='content ' * 20,
                                          org=self.uganda,
                                          created_by=self.admin,
                                          modified_by=self.admin)

        self.assertEquals(self.story.teaser(self.story.summary, 30), "")
        self.assertEquals(self.story.teaser(self.story.content, 30), self.story.content)

        self.story.content = 'content ' * 250
        self.story.save()

        self.assertEquals(self.story.teaser(self.story.summary, 30), "")
        self.assertEquals(self.story.teaser(self.story.content, 30), "content " * 30 + "..")
        self.assertEquals(self.story.long_teaser(), "content " * 100 + "..")
        self.assertEquals(self.story.short_teaser(), "content " * 40 +  "..")

        self.story.summary = "summary " * 150
        self.story.save()

        self.assertEquals(self.story.long_teaser(), "summary " * 100 + "..")
        self.assertEquals(self.story.short_teaser(), "summary " * 40 + "..")

        story_image_1 = StoryImage.objects.create(name='image 1',
                                                  story=self.story,
                                                  image='',
                                                  created_by=self.admin,
                                                  modified_by=self.admin)

        self.assertFalse(self.story.get_featured_images())

        story_image_1.image = 'stories/someimage.jpg'
        story_image_1.is_active = False
        story_image_1.save()

        self.assertFalse(self.story.get_featured_images())

        story_image_1.is_active = True
        story_image_1.save()

        self.assertTrue(self.story.get_featured_images())
        self.assertEquals(len(self.story.get_featured_images()), 1)
        self.assertTrue(story_image_1 in self.story.get_featured_images())

        self.assertEquals(self.story.get_category_image(), 'stories/someimage.jpg')
        self.assertEquals(self.story.get_image(), 'stories/someimage.jpg')

        self.story.category = self.health_uganda
        self.story.save()

        self.assertEquals(self.story.get_category_image(), 'stories/someimage.jpg')
        self.assertEquals(self.story.get_image(), 'stories/someimage.jpg')

        category_image1 = CategoryImage.objects.create(category=self.health_uganda,
                                                       name='image 1',
                                                       image='categories/some_image.jpg',
                                                       created_by=self.admin,
                                                       modified_by=self.admin)

        self.assertEquals(self.story.get_category_image(), 'categories/some_image.jpg')
        self.assertEquals(self.story.get_image(), 'stories/someimage.jpg')

        story_image_1.is_active = False
        story_image_1.save()

        self.assertEquals(self.story.get_category_image(), 'categories/some_image.jpg')
        self.assertEquals(self.story.get_image(), 'categories/some_image.jpg')

        self.health_uganda.is_active = False
        self.health_uganda.save()

        self.assertFalse(self.story.get_category_image())
        self.assertFalse(self.story.get_image(), 'categories/some_image.jpg')

    def test_create_story(self):
        create_url = reverse('stories.story_create')

        response = self.client.get(create_url)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(create_url, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse(settings.SITE_CHOOSER_VIEW_NAME))

        self.login(self.admin)
        response = self.client.get(create_url)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(create_url, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse(settings.SITE_CHOOSER_VIEW_NAME))

        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertTrue('form' in response.context)
        fields = response.context['form'].fields
        self.assertEquals(len(fields), 8)
        self.assertTrue('loc' in fields)
        self.assertTrue('title' in fields)
        self.assertTrue('featured' in fields)
        self.assertTrue('summary' in fields)
        self.assertTrue('content' in fields)
        self.assertTrue('video_id' in fields)
        self.assertTrue('tags' in fields)
        self.assertTrue('category' in fields)

        self.assertEquals(len(fields['category'].choices.queryset), 1)

        response = self.client.post(create_url, dict(), SERVER_NAME='uganda.ureport.io')
        self.assertTrue(response.context['form'].errors)
        errors = response.context['form'].errors
        self.assertTrue('title' in errors)
        self.assertTrue('content' in errors)
        self.assertTrue('category' in errors)

        post_data = dict(title='foo', content='bar', category=self.health_uganda.pk, featured=True, summary='baz',
                         video_id='yt_id', tags='   first SECOND third')

        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        story = Story.objects.get()
        self.assertEquals(response.request['PATH_INFO'], reverse('stories.story_images', args=[story.pk]))
        self.assertEquals(story.title, 'foo')
        self.assertEquals(story.content, 'bar')
        self.assertEquals(story.category, self.health_uganda)
        self.assertTrue(story.featured)
        self.assertEquals(story.summary, 'baz')
        self.assertEquals(story.video_id, 'yt_id')
        self.assertEquals(story.tags, ' first second third ')

    def test_update_story(self):
        story1 = Story.objects.create(title='foo',
                                     content='bar',
                                     category=self.health_uganda,
                                     org=self.uganda,
                                     created_by=self.admin,
                                     modified_by=self.admin)

        story2 = Story.objects.create(title='foo',
                                     content='bar',
                                     category=self.education_nigeria,
                                     org=self.nigeria,
                                     created_by=self.admin,
                                     modified_by=self.admin)

        update_url_uganda = reverse('stories.story_update', args=[story1.pk])
        update_url_nigeria = reverse('stories.story_update', args=[story2.pk])

        response = self.client.get(update_url_uganda)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(update_url_uganda, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse(settings.SITE_CHOOSER_VIEW_NAME))

        response = self.client.get(update_url_nigeria)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(update_url_nigeria, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse(settings.SITE_CHOOSER_VIEW_NAME))

        response = self.client.get(update_url_uganda, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        response = self.client.get(update_url_nigeria, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(update_url_uganda)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(update_url_uganda, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse(settings.SITE_CHOOSER_VIEW_NAME))

        response = self.client.get(update_url_nigeria)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(update_url_nigeria, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse(settings.SITE_CHOOSER_VIEW_NAME))

        response = self.client.get(update_url_nigeria, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        response = self.client.get(update_url_uganda, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertTrue('form' in response.context)
        fields = response.context['form'].fields

        self.assertEquals(len(fields), 9)
        self.assertTrue('loc' in fields)
        self.assertTrue('is_active' in fields)
        self.assertTrue('title' in fields)
        self.assertTrue('featured' in fields)
        self.assertTrue('summary' in fields)
        self.assertTrue('content' in fields)
        self.assertTrue('video_id' in fields)
        self.assertTrue('tags' in fields)
        self.assertTrue('category' in fields)
        self.assertEquals(len(fields['category'].choices.queryset), 1)

        response = self.client.post(update_url_uganda, dict(), SERVER_NAME='uganda.ureport.io')

        self.assertTrue(response.context['form'].errors)
        errors = response.context['form'].errors
        self.assertTrue('title' in errors)
        self.assertTrue('content' in errors)

        post_data = dict(title='foo updated', content='bar updated', category=self.health_uganda.pk, featured=True,
                         summary='baz updated', video_id='yt_idUpdated', tags='   first SECOND third UPDATED')
        response = self.client.post(update_url_uganda, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        updated_story = Story.objects.get(pk=story1.pk)
        self.assertEquals(response.request['PATH_INFO'], reverse('stories.story_list'))

        self.assertEquals(updated_story.title, 'foo updated')
        self.assertEquals(updated_story.content, 'bar updated')
        self.assertEquals(updated_story.category, self.health_uganda)
        self.assertTrue(updated_story.featured)
        self.assertEquals(updated_story.summary, 'baz updated')
        self.assertEquals(updated_story.video_id, 'yt_idUpdated')
        self.assertEquals(updated_story.tags, ' first second third updated ')

    def test_list_stories(self):
        story1 = Story.objects.create(title='foo',
                                      content='bar',
                                      category=self.health_uganda,
                                      org=self.uganda,
                                      created_by=self.admin,
                                      modified_by=self.admin)

        story2 = Story.objects.create(title='foo',
                                      content='bar',
                                      category=self.education_nigeria,
                                      org=self.nigeria,
                                      created_by=self.admin,
                                      modified_by=self.admin)

        list_url = reverse('stories.story_list')

        response = self.client.get(list_url)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(list_url, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse(settings.SITE_CHOOSER_VIEW_NAME))

        self.login(self.admin)
        response = self.client.get(list_url)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(list_url, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse(settings.SITE_CHOOSER_VIEW_NAME))

        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['object_list']), 1)
        self.assertTrue(story1 in response.context['object_list'])
        self.assertFalse(story2 in response.context['object_list'])

        self.assertTrue(reverse('stories.story_images', args=[story1.pk]) in response.content)

    def test_images_story(self):
        story1 = Story.objects.create(title='foo',
                                      content='bar',
                                      category=self.health_uganda,
                                      org=self.uganda,
                                      created_by=self.admin,
                                      modified_by=self.admin)

        story2 = Story.objects.create(title='foo',
                                      content='bar',
                                      category=self.education_nigeria,
                                      org=self.nigeria,
                                      created_by=self.admin,
                                      modified_by=self.admin)

        images_url_uganda = reverse('stories.story_images', args=[story1.pk])
        images_url_nigeria = reverse('stories.story_images', args=[story2.pk])


        response = self.client.get(images_url_uganda)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(images_url_uganda, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse(settings.SITE_CHOOSER_VIEW_NAME))

        response = self.client.get(images_url_nigeria)
        self.assertEquals(response.status_code, 302)
        response = self.client.get(images_url_nigeria, follow=True)
        self.assertEquals(response.request['PATH_INFO'], reverse(settings.SITE_CHOOSER_VIEW_NAME))

        response = self.client.get(images_url_uganda, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        response = self.client.get(images_url_nigeria, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(images_url_nigeria, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        response = self.client.get(images_url_uganda, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertTrue('form' in response.context)
        self.assertEquals(len(response.context['form'].fields), 3)
        for field in response.context['form'].fields:
            self.assertFalse(response.context['form'].fields[field].initial)

        self.assertFalse(StoryImage.objects.filter(story=story1))

        upload = open("%s/image.jpg" % settings.TESTFILES_DIR, "r")
        post_data = dict(image_1=upload)
        response = self.client.post(images_url_uganda, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertTrue(StoryImage.objects.filter(story=story1))
        self.assertEquals(StoryImage.objects.filter(story=story1).count(), 1)

        response = self.client.get(images_url_uganda, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(len(response.context['form'].fields), 3)
        self.assertTrue(response.context['form'].fields['image_1'].initial)

        upload = open("%s/image.jpg" % settings.TESTFILES_DIR, "r")
        post_data = dict(image_1=upload)
        response = self.client.post(images_url_uganda, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertTrue(StoryImage.objects.filter(story=story1))
        self.assertEquals(StoryImage.objects.filter(story=story1).count(), 1)

        self.assertEquals(response.request['PATH_INFO'], reverse('stories.story_list'))

        self.clear_uploads()

class DashBlockTypeTest(DashTest):
    def setUp(self):
        super(DashBlockTypeTest, self).setUp()
        self.uganda = self.create_org('uganda', self.admin)
        self.nigeria = self.create_org('nigeria', self.admin)

    def test_create_dashblocktype(self):
        create_url = reverse('dashblocks.dashblocktype_create')

        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.superuser)
        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertTrue('form' in response.context)
        fields = response.context['form'].fields
        self.assertEquals(len(fields), 13)

        response = self.client.post(create_url, dict(), SERVER_NAME='uganda.uerport.io')
        self.assertTrue(response.context['form'].errors)
        errors = response.context['form'].errors
        self.assertEquals(len(errors), 2)
        self.assertTrue('name' in errors)
        self.assertTrue('slug' in errors)

        post_data = dict(name='Test Pages', slug='test_pages', description='foo')
        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        dashblocktype = DashBlockType.objects.get()
        self.assertEquals(dashblocktype.name, 'Test Pages')
        self.assertEquals(dashblocktype.slug, 'test_pages')
        self.assertEquals(dashblocktype.description, 'foo')
        self.assertFalse(dashblocktype.has_title)
        self.assertFalse(dashblocktype.has_image)
        self.assertFalse(dashblocktype.has_rich_text)
        self.assertFalse(dashblocktype.has_summary)
        self.assertFalse(dashblocktype.has_link)
        self.assertFalse(dashblocktype.has_color)
        self.assertFalse(dashblocktype.has_gallery)
        self.assertFalse(dashblocktype.has_tags)
        self.assertFalse(dashblocktype.has_video)

        self.assertEquals(dashblocktype.__unicode__(), 'Test Pages')

    def test_list_dashblocktype(self):
        list_url = reverse('dashblocks.dashblocktype_list')

        dashblock_type = DashBlockType.objects.create(name='Test', slug='test',
                                                      description='foo',
                                                      has_title=True,
                                                      has_image=True,
                                                      has_rich_text=True,
                                                      has_summary=True,
                                                      has_link=True,
                                                      has_color=False,
                                                      has_video=False,
                                                      has_tags=False,
                                                      has_gallery=False,
                                                      created_by=self.admin,
                                                      modified_by=self.admin)

        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.superuser)
        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['fields']), 3)
        self.assertTrue('name' in response.context['fields'])
        self.assertTrue('slug' in response.context['fields'])
        self.assertTrue('description' in response.context['fields'])
        self.assertTrue(dashblock_type in response.context['object_list'])

    def test_update_dashblocktype(self):

        dashblock_type = DashBlockType.objects.create(name='Test', slug='test',
                                                      description='foo',
                                                      has_title=True,
                                                      has_image=True,
                                                      has_rich_text=True,
                                                      has_summary=True,
                                                      has_link=True,
                                                      has_color=False,
                                                      has_video=False,
                                                      has_tags=False,
                                                      has_gallery=False,
                                                      created_by=self.admin,
                                                      modified_by=self.admin)

        update_url = reverse('dashblocks.dashblocktype_update', args=[dashblock_type.pk])

        response = self.client.get(update_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(update_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.superuser)
        response = self.client.get(update_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertTrue('form' in response.context)
        fields = response.context['form'].fields
        self.assertEquals(len(fields), 14)

        response = self.client.post(update_url, dict(), SERVER_NAME='uganda.ureport.io')
        self.assertTrue(response.context['form'].errors)
        errors = response.context['form'].errors
        self.assertEquals(len(errors), 2)
        self.assertTrue('name' in errors)
        self.assertTrue('slug' in errors)

        post_data = dict(is_active=True, name='foo', slug='bar', description='baz', has_rich_text=True, has_video=True)
        response = self.client.post(update_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertFalse('form' in response.context)
        updated_dashblock_type = DashBlockType.objects.get(pk=dashblock_type.pk)
        self.assertEquals(updated_dashblock_type.name, 'foo')
        self.assertEquals(updated_dashblock_type.slug, 'bar')
        self.assertEquals(updated_dashblock_type.description, 'baz')
        self.assertTrue(updated_dashblock_type.has_rich_text)
        self.assertTrue(updated_dashblock_type.has_video)
        self.assertFalse(updated_dashblock_type.has_title)
        self.assertFalse(updated_dashblock_type.has_summary)
        self.assertFalse(updated_dashblock_type.has_color)
        self.assertFalse(updated_dashblock_type.has_tags)
        self.assertFalse(updated_dashblock_type.has_gallery)


class DashBlockTest(DashTest):
    def setUp(self):
        super(DashBlockTest, self).setUp()
        self.uganda = self.create_org('uganda', self.admin)
        self.nigeria = self.create_org('nigeria', self.admin)

        self.type_foo = DashBlockType.objects.create(name='Foo', slug='foo',
                                                      description='foo description',
                                                      has_title=True,
                                                      has_image=True,
                                                      has_rich_text=True,
                                                      has_summary=True,
                                                      has_link=True,
                                                      has_color=False,
                                                      has_video=False,
                                                      has_tags=True,
                                                      has_gallery=False,
                                                      created_by=self.admin,
                                                      modified_by=self.admin)

        self.type_bar = DashBlockType.objects.create(name='Bar', slug='bar',
                                                     description='bar description',
                                                     has_title=False,
                                                     has_image=False,
                                                     has_rich_text=False,
                                                     has_summary=False,
                                                     has_link=False,
                                                     has_color=False,
                                                     has_video=False,
                                                     has_tags=True,
                                                     has_gallery=False,
                                                     created_by=self.admin,
                                                     modified_by=self.admin)


    def test_dashblock_model(self):
        dashblock1 = DashBlock.objects.create(dashblock_type=self.type_foo,
                                              org=self.uganda,
                                              title='First',
                                              content='First content',
                                              summary='first summary',
                                              created_by=self.admin,
                                              modified_by=self.admin)

        self.assertEquals(dashblock1.__unicode__(), 'First')


        dashblock2 = DashBlock.objects.create(dashblock_type=self.type_bar,
                                              org=self.uganda,
                                              content='Bar content',
                                              summary='bar summary here',
                                              created_by=self.admin,
                                              modified_by=self.admin)

        self.assertEquals(dashblock2.__unicode__(), 'Bar - %d' % dashblock2.pk)

        self.assertEquals(dashblock1.teaser(dashblock1.content, 1), 'First ...')
        self.assertEquals(dashblock1.teaser(dashblock1.content, 10), 'First content')

        self.assertEquals(dashblock1.long_content_teaser(), 'First content')
        self.assertEquals(dashblock1.short_content_teaser(), 'First content')
        self.assertEquals(dashblock1.long_summary_teaser(), 'first summary')
        self.assertEquals(dashblock1.short_summary_teaser(), 'first summary')

        dashblock1.content = 'ab ' * 150
        dashblock1.summary = 'cd ' * 120
        dashblock1.save()

        self.assertEquals(dashblock1.long_content_teaser(), 'ab ' * 100 + "...")
        self.assertEquals(dashblock1.short_content_teaser(), 'ab ' * 40 + "...")
        self.assertEquals(dashblock1.long_summary_teaser(), 'cd ' * 100 + "...")
        self.assertEquals(dashblock1.short_summary_teaser(), 'cd ' * 40 + "...")

    def test_create_dashblock(self):
        create_url = reverse('dashblocks.dashblock_create')

        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertTrue('form' in response.context)
        fields = response.context['form'].fields
        self.assertEquals(len(fields), 11)
        self.assertTrue('dashblock_type' in fields)
        self.assertTrue('title' in fields)
        self.assertTrue('summary' in fields)
        self.assertTrue('content' in fields)
        self.assertTrue('image' in fields)
        self.assertTrue('color' in fields)
        self.assertTrue('link' in fields)
        self.assertTrue('video_id' in fields)
        self.assertTrue('tags' in fields)
        self.assertTrue('priority' in fields)
        self.assertTrue('loc' in fields)
        self.assertFalse('gallery' in fields)
        self.assertFalse('org' in fields)

        self.assertEquals(fields['priority'].initial, 0)
        self.assertIsNone(response.context['type'])

        response = self.client.post(create_url, dict(), SERVER_NAME='uganda.ureport.io')
        self.assertTrue(response.context['form'].errors)
        self.assertTrue('dashblock_type' in response.context['form'].errors)
        self.assertTrue('priority' in response.context['form'].errors)

        post_data = dict(dashblock_type=self.type_bar.pk, priority=2, tags='   first SECOND four   ')
        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')

        dashblock = DashBlock.objects.get()
        self.assertEquals(dashblock.priority, 2)
        self.assertEquals(dashblock.dashblock_type, self.type_bar)
        self.assertEquals(dashblock.tags, ' first second four ')

        self.assertEquals(response.request['PATH_INFO'], reverse('dashblocks.dashblock_list'))
        self.assertEquals(response.request['QUERY_STRING'], "type=%d" % self.type_bar.pk)

        response = self.client.get(create_url + "?type=%d" % self.type_bar.pk, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertTrue('form' in response.context)
        fields = response.context['form'].fields
        self.assertEquals(len(fields), 4)
        self.assertTrue('tags' in fields)
        self.assertTrue('priority' in fields)
        self.assertTrue('loc' in fields)
        self.assertTrue('content' in fields)
        self.assertTrue(response.context['type'])
        self.assertEquals(response.context['type'], self.type_bar)
        #self.assertEquals(fields['priority'].initial, 3)

        response = self.client.get(create_url + "?type=%d" % self.type_foo.pk, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertTrue('form' in response.context)
        fields = response.context['form'].fields
        self.assertEquals(len(fields), 8)
        self.assertTrue('title' in fields)
        self.assertTrue('summary' in fields)
        self.assertTrue('content' in fields)
        self.assertTrue('image' in fields)
        self.assertTrue('link' in fields)
        self.assertTrue('tags' in fields)
        self.assertTrue('priority' in fields)
        self.assertTrue('loc' in fields)
        self.assertFalse('video_id' in fields)
        self.assertFalse('color' in fields)
        self.assertFalse('gallery' in fields)
        self.assertFalse('org' in fields)
        self.assertFalse('dashblock_type' in fields)

        self.assertTrue(response.context['type'])
        self.assertEquals(response.context['type'], self.type_foo)

        post_data = dict(title='kigali', content='kacyiru', tags=' Gasabo KACYIRU Umujyi   ', priority=0)
        response = self.client.post(create_url + "?type=%d" % self.type_foo.pk, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        new_dashblock = DashBlock.objects.get(title='kigali')
        self.assertEquals(new_dashblock.dashblock_type, self.type_foo)
        self.assertEquals(new_dashblock.org, self.uganda)
        self.assertEquals(new_dashblock.tags, ' gasabo kacyiru umujyi ')
        self.assertEquals(new_dashblock.title, 'kigali')
        self.assertEquals(new_dashblock.content, 'kacyiru')

    def test_update_dashblock(self):
        dashblock1 = DashBlock.objects.create(dashblock_type=self.type_foo,
                                              org=self.uganda,
                                              title='First',
                                              content='First content',
                                              summary='first summary',
                                              created_by=self.admin,
                                              modified_by=self.admin)

        update_url = reverse('dashblocks.dashblock_update', args=[dashblock1.pk])

        response = self.client.get(update_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(update_url, SERVER_NAME='uganda.ureport.io')

        self.assertEquals(response.status_code, 200)
        self.assertTrue('form' in response.context)
        fields = response.context['form'].fields
        self.assertEquals(len(fields), 9)
        self.assertTrue('is_active' in fields)
        self.assertTrue('title' in fields)
        self.assertTrue('summary' in fields)
        self.assertTrue('content' in fields)
        self.assertTrue('image' in fields)
        self.assertTrue('link' in fields)
        self.assertTrue('priority' in fields)
        self.assertTrue('loc' in fields)
        self.assertTrue('tags' in fields)
        self.assertFalse('video_id' in fields)
        self.assertFalse('color' in fields)
        self.assertFalse('gallery' in fields)
        self.assertFalse('org' in fields)
        self.assertFalse('dashblock_type' in fields)

        self.assertTrue(response.context['type'])
        self.assertEquals(response.context['type'], self.type_foo)

        response = self.client.post(update_url, dict(), SERVER_NAME='uganda.ureport.io')
        self.assertTrue('form' in response.context)
        errors = response.context['form'].errors

        self.assertTrue('priority' in response.context['form'].errors)

        post_data = dict(title='kigali', content='kacyiru', tags=' Gasabo KACYIRU Umujyi   ', priority=0)
        response = self.client.post(update_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        self.assertFalse('form' in response.context)
        updated_dashblock = DashBlock.objects.get(pk=dashblock1.pk)
        self.assertEquals(updated_dashblock.dashblock_type, self.type_foo)
        self.assertEquals(updated_dashblock.org, self.uganda)
        self.assertEquals(updated_dashblock.tags, ' gasabo kacyiru umujyi ')
        self.assertEquals(updated_dashblock.title, 'kigali')
        self.assertEquals(updated_dashblock.content, 'kacyiru')

        self.type_foo.has_tags = False
        self.type_foo.save()

        response = self.client.get(update_url, SERVER_NAME='uganda.ureport.io')

        self.assertEquals(response.status_code, 200)
        self.assertTrue('form' in response.context)
        fields = response.context['form'].fields
        self.assertEquals(len(fields), 8)
        self.assertTrue('is_active' in fields)
        self.assertTrue('title' in fields)
        self.assertTrue('summary' in fields)
        self.assertTrue('content' in fields)
        self.assertTrue('image' in fields)
        self.assertTrue('link' in fields)
        self.assertTrue('priority' in fields)
        self.assertTrue('loc' in fields)
        self.assertFalse('dashblock_type' in fields)
        self.assertFalse('video_id' in fields)
        self.assertFalse('color' in fields)
        self.assertFalse('gallery' in fields)
        self.assertFalse('org' in fields)
        self.assertFalse('tags' in fields)


    def test_list_dashblock(self):
        list_url = reverse('dashblocks.dashblock_list')

        dashblock1 = DashBlock.objects.create(dashblock_type=self.type_foo,
                                              org=self.uganda,
                                              title='First',
                                              content='First content',
                                              summary='first summary',
                                              created_by=self.admin,
                                              modified_by=self.admin)


        dashblock2 = DashBlock.objects.create(dashblock_type=self.type_bar,
                                              org=self.uganda,
                                              content='Bar content',
                                              summary='bar summary here',
                                              created_by=self.admin,
                                              modified_by=self.admin)

        dashblock3 = DashBlock.objects.create(dashblock_type=self.type_foo,
                                              org=self.nigeria,
                                              title='third',
                                              content='third content',
                                              summary='third summary',
                                              created_by=self.admin,
                                              modified_by=self.admin)

        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.admin)
        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['object_list']), 2)
        self.assertTrue(dashblock1 in response.context['object_list'])
        self.assertTrue(dashblock2 in response.context['object_list'])
        self.assertFalse(dashblock3 in response.context['object_list'])
        self.assertEquals(len(response.context['fields']), 4)
        self.assertTrue('tags' in response.context['fields'])
        self.assertTrue('title' in response.context['fields'])
        self.assertTrue('dashblock_type' in response.context['fields'])
        self.assertTrue('priority' in response.context['fields'])

        self.assertEquals(len(response.context['types']), 2)
        self.assertTrue(self.type_foo in response.context['types'])
        self.assertTrue(self.type_bar in response.context['types'])

        response = self.client.get(list_url + "?type=%d" % self.type_bar.pk, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertEquals(len(response.context['object_list']), 1)
        self.assertFalse(dashblock1 in response.context['object_list'])
        self.assertTrue(dashblock2 in response.context['object_list'])
        self.assertFalse(dashblock3 in response.context['object_list'])

        self.assertTrue(unicode(dashblock2) in response.content)

        self.assertEquals(len(response.context['fields']), 4)
        self.assertTrue('tags' in response.context['fields'])
        self.assertTrue('title' in response.context['fields'])
        self.assertTrue('dashblock_type' in response.context['fields'])
        self.assertTrue('priority' in response.context['fields'])
        self.assertEquals(len(response.context['types']), 2)
        self.assertTrue(self.type_foo in response.context['types'])
        self.assertTrue(self.type_bar in response.context['types'])

        self.type_bar.has_tags = False
        self.type_bar.save()

        response = self.client.get(list_url + "?type=%d" % self.type_bar.pk, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(len(response.context['fields']), 3)
        self.assertFalse('tags' in response.context['fields'])
        self.assertTrue('title' in response.context['fields'])
        self.assertTrue('dashblock_type' in response.context['fields'])
        self.assertTrue('priority' in response.context['fields'])


        self.type_bar.is_active = False
        self.type_bar.save()

        response = self.client.get(list_url + "?type=%d" % self.type_bar.pk, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(response.status_code, 200)
        self.assertFalse(response.context['object_list'])
        self.assertFalse(dashblock1 in response.context['object_list'])
        self.assertFalse(dashblock2 in response.context['object_list'])
        self.assertFalse(dashblock3 in response.context['object_list'])
        self.assertEquals(len(response.context['types']), 1)
        self.assertTrue(self.type_foo in response.context['types'])
        self.assertFalse(self.type_bar in response.context['types'])

        response = self.client.get(list_url + "?slug=%s" % self.type_foo.slug, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(len(response.context['object_list']), 1)
        self.assertTrue(dashblock1 in response.context['object_list'])
        self.assertFalse(dashblock2 in response.context['object_list'])
        self.assertFalse(dashblock3 in response.context['object_list'])
        self.assertEquals(response.context['filtered_type'], self.type_foo)
        self.assertEquals(len(response.context['types']), 1)
        self.assertTrue(self.type_foo in response.context['types'])
        self.assertFalse(self.type_bar in response.context['types'])

    def test_dashblock_image(self):
        dashblock1 = DashBlock.objects.create(dashblock_type=self.type_foo,
                                              org=self.uganda,
                                              title='First',
                                              content='First content',
                                              summary='first summary',
                                              created_by=self.admin,
                                              modified_by=self.admin)

        create_url = reverse('dashblocks.dashblockimage_create') + "?dashblock=%d" % dashblock1.pk

        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertLoginRedirect(response)

        self.login(self.superuser)
        response = self.client.get(create_url, SERVER_NAME='uganda.ureport.io')
        self.assertTrue('form' in response.context)

        response = self.client.post(create_url, dict(), SERVER_NAME='uganda.ureport.io')
        self.assertTrue(response.context['form'].errors)

        upload = open("%s/image.jpg" % settings.TESTFILES_DIR, "r")
        post_data = dict(dashblock=dashblock1.pk, image=upload, caption='image caption')

        response = self.client.post(create_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')
        dashblock_image = DashBlockImage.objects.get()

        self.assertEquals(dashblock_image.dashblock, dashblock1)
        self.assertEquals(dashblock_image.caption, 'image caption')

        self.assertEquals(response.request['PATH_INFO'], reverse('dashblocks.dashblock_update', args=[dashblock1.pk]))

        update_url = reverse('dashblocks.dashblockimage_update', args=[dashblock_image.pk])

        upload = open("%s/image.jpg" % settings.TESTFILES_DIR, "r")
        post_data = dict(dashblock=dashblock1.pk, image=upload, caption='image updated caption')
        response = self.client.post(update_url, post_data, follow=True, SERVER_NAME='uganda.ureport.io')

        self.assertEquals(DashBlockImage.objects.count(), 1)
        updated_block_image = DashBlockImage.objects.get(pk=dashblock_image.pk)
        self.assertEquals(updated_block_image.caption, 'image updated caption')
        self.assertEquals(response.request['PATH_INFO'], reverse('dashblocks.dashblock_update', args=[dashblock1.pk]))

        list_url = reverse('dashblocks.dashblockimage_list')
        response = self.client.get(list_url, SERVER_NAME='uganda.ureport.io')
        self.assertEquals(len(response.context['object_list']), 1)
        self.assertTrue(updated_block_image in response.context['object_list'])

        self.clear_uploads()

    def test_template_tags(self):
        dashblock1 = DashBlock.objects.create(dashblock_type=self.type_foo,
                                              org=self.uganda,
                                              title='First',
                                              content='First content',
                                              summary='first summary',
                                              created_by=self.admin,
                                              modified_by=self.admin)


        dashblock2 = DashBlock.objects.create(dashblock_type=self.type_bar,
                                              org=self.uganda,
                                              content='Bar content',
                                              summary='bar summary here',
                                              created_by=self.admin,
                                              modified_by=self.admin)

        dashblock3 = DashBlock.objects.create(dashblock_type=self.type_foo,
                                              org=self.nigeria,
                                              title='third',
                                              content='third content',
                                              summary='third summary',
                                              created_by=self.admin,
                                              modified_by=self.admin)

        context = dict()
        self.assertEquals(load_qbs(context, None, ''), '')
        self.assertFalse(context)

        self.assertEquals(load_qbs(context, self.uganda, 'invalid_slug'),
                          getattr(settings, 'DASHBLOCK_STRING_IF_INVALID', '<b><font color="red">DashBlockType with slug: %s not found</font></b>') % 'invalid_slug')
        self.assertFalse(context)

        self.assertEquals(load_qbs(context, self.uganda, 'foo'), '')
        self.assertTrue(context)
        self.assertTrue('foo' in context)
        self.assertTrue(dashblock1 in context['foo'])
        self.assertFalse(dashblock2 in context['foo'])
        self.assertFalse(dashblock3 in context['foo'])

        dashblock4 = DashBlock.objects.create(dashblock_type=self.type_foo,
                                              org=self.uganda,
                                              title='Fourth',
                                              content='Fourth content',
                                              summary='fourth summary',
                                              created_by=self.admin,
                                              modified_by=self.admin)

        dashblock1.tags = ' kigali gasabo '
        dashblock1.save()

        dashblock4.tags = ' kigali kacyiru '
        dashblock4.save()

        self.assertEquals(load_qbs(context, self.uganda, 'foo'), '')
        self.assertTrue(context)
        self.assertTrue('foo' in context)
        self.assertTrue(dashblock1 in context['foo'])
        self.assertFalse(dashblock2 in context['foo'])
        self.assertFalse(dashblock3 in context['foo'])
        self.assertTrue(dashblock4 in context['foo'])

        self.assertEquals(load_qbs(context, self.uganda, 'foo', 'kigali'), '')
        self.assertTrue(context)
        self.assertTrue('foo' in context)
        self.assertTrue(dashblock1 in context['foo'])
        self.assertFalse(dashblock2 in context['foo'])
        self.assertFalse(dashblock3 in context['foo'])
        self.assertTrue(dashblock4 in context['foo'])

        self.assertEquals(load_qbs(context, self.uganda, 'foo', 'gasabo'), '')
        self.assertTrue(context)
        self.assertTrue('foo' in context)
        self.assertTrue(dashblock1 in context['foo'])
        self.assertFalse(dashblock2 in context['foo'])
        self.assertFalse(dashblock3 in context['foo'])
        self.assertFalse(dashblock4 in context['foo'])
