from datetime import datetime

from django.conf import settings
from django.template import Context, loader
from django.utils.datastructures import SortedDict

import commonware.log
import django_tables as tables
import jinja2
from tower import ugettext_lazy as _lazy

import amo
from amo.helpers import absolutify, timesince
from amo.urlresolvers import reverse
from amo.utils import send_mail as amo_send_mail

# TODO: Remove.
from editors.helpers import ItemStateTable, ReviewFiles

from mkt.webapps.models import Webapp


log = commonware.log.getLogger('z.mailer')

NOMINATED_STATUSES = (amo.STATUS_NOMINATED, amo.STATUS_LITE_AND_NOMINATED)
PRELIMINARY_STATUSES = (amo.STATUS_UNREVIEWED, amo.STATUS_LITE)
PENDING_STATUSES = (amo.STATUS_BETA, amo.STATUS_DISABLED, amo.STATUS_LISTED,
                    amo.STATUS_NULL, amo.STATUS_PENDING, amo.STATUS_PUBLIC)


def send_mail(template, subject, emails, context, perm_setting=None):
    template = loader.get_template(template)
    amo_send_mail(subject, template.render(Context(context, autoescape=False)),
                  recipient_list=emails, from_email=settings.EDITORS_EMAIL,
                  use_blacklist=False, perm_setting=perm_setting)


class WebappQueueTable(tables.ModelTable, ItemStateTable):
    name = tables.Column(verbose_name=_lazy(u'App'))
    created = tables.Column(verbose_name=_lazy(u'Waiting Time'))
    abuse_reports__count = tables.Column(verbose_name=_lazy(u'Abuse Reports'))

    def render_name(self, row):
        url = '%s?num=%s' % (self.review_url(row), self.item_number)
        self.increment_item()
        return u'<a href="%s">%s</a>' % (url, jinja2.escape(row.name))

    def render_abuse_reports__count(self, row):
        url = reverse('editors.abuse_reports', args=[row.slug])
        return u'<a href="%s">%s</a>' % (jinja2.escape(url),
                                         row.abuse_reports__count)

    def render_created(self, row):
        return timesince(row.created)

    @classmethod
    def translate_sort_cols(cls, colname):
        return colname

    @classmethod
    def default_order_by(cls):
        return 'created'

    @classmethod
    def review_url(cls, row):
        return reverse('reviewers.app_review', args=[row.app_slug])

    class Meta:
        sortable = True
        model = Webapp
        columns = ['name', 'created', 'abuse_reports__count']


class ReviewBase:

    def __init__(self, request, addon, version, review_type):
        self.request = request
        self.user = self.request.user
        self.addon = addon
        self.version = version
        self.review_type = review_type
        self.files = None

    def set_addon(self, **kw):
        """Alters addon and sets reviewed timestamp on version."""
        self.addon.update(**kw)
        self.version.update(reviewed=datetime.now())

    def set_files(self, status, files, copy_to_mirror=False,
                  hide_disabled_file=False):
        """Change the files to be the new status
        and copy, remove from the mirror as appropriate."""
        for file in files:
            file.datestatuschanged = datetime.now()
            file.reviewed = datetime.now()
            if copy_to_mirror:
                file.copy_to_mirror()
            if hide_disabled_file:
                file.hide_disabled_file()
            file.status = status
            file.save()

    def log_action(self, action):
        details = {'comments': self.data['comments'],
                   'reviewtype': self.review_type}
        if self.files:
            details['files'] = [f.id for f in self.files]

        amo.log(action, self.addon, self.version, user=self.user.get_profile(),
                created=datetime.now(), details=details)

    def notify_email(self, template, subject):
        """Notify the authors that their addon has been reviewed."""
        emails = [a.email for a in self.addon.authors.all()]
        data = self.data.copy()
        data.update(self.get_context_data())
        data['tested'] = ''
        os, app = data.get('operating_systems'), data.get('applications')
        if os and app:
            data['tested'] = 'Tested on %s with %s' % (os, app)
        elif os and not app:
            data['tested'] = 'Tested on %s' % os
        elif not os and app:
            data['tested'] = 'Tested with %s' % app
        data['addon_type'] = (_lazy('app')
                              if self.addon.type == amo.ADDON_WEBAPP
                              else _lazy('add-on'))
        send_mail('editors/emails/%s.ltxt' % template,
                   subject % (self.addon.name, self.version.version),
                   emails, Context(data), perm_setting='editor_reviewed')

    def get_context_data(self):
        return {'name': self.addon.name,
                'number': self.version.version,
                'reviewer': (self.request.user.get_profile().display_name),
                'addon_url': absolutify(
                    self.addon.get_url_path(add_prefix=False)),
                'review_url': absolutify(reverse('editors.review',
                                                 args=[self.addon.pk],
                                                 add_prefix=False)),
                'comments': self.data['comments'],
                'SITE_URL': settings.SITE_URL}

    def request_information(self):
        """Send a request for information to the authors."""
        emails = [a.email for a in self.addon.authors.all()]
        self.log_action(amo.LOG.REQUEST_INFORMATION)
        self.version.update(has_info_request=True)
        log.info(u'Sending request for information for %s to %s' %
                 (self.addon, emails))
        send_mail('editors/emails/info.ltxt',
                   u'Mozilla Add-ons: %s %s' %
                   (self.addon.name, self.version.version),
                   emails, Context(self.get_context_data()),
                   perm_setting='individual_contact')

    def send_super_mail(self):
        self.log_action(amo.LOG.REQUEST_SUPER_REVIEW)
        log.info(u'Super review requested for %s' % (self.addon))
        send_mail('editors/emails/super_review.ltxt',
                   u'Super review requested: %s' % (self.addon.name),
                   [settings.SENIOR_EDITORS_EMAIL],
                   Context(self.get_context_data()))


class ReviewAddon(ReviewBase):

    def set_data(self, data):
        self.data = data
        self.files = self.version.files.all()

    def process_public(self):
        """Set an addon to public."""
        if self.review_type == 'preliminary':
            raise AssertionError('Preliminary addons cannot be made public.')

        # Save files first, because set_addon checks to make sure there
        # is at least one public file or it won't make the addon public.
        self.set_files(amo.STATUS_PUBLIC, self.version.files.all(),
                       copy_to_mirror=True)
        self.set_addon(highest_status=amo.STATUS_PUBLIC,
                       status=amo.STATUS_PUBLIC)

        self.log_action(amo.LOG.APPROVE_VERSION)
        self.notify_email('%s_to_public' % self.review_type,
                          u'Mozilla Add-ons: %s %s Fully Reviewed')

        log.info(u'Making %s public' % (self.addon))
        log.info(u'Sending email for %s' % (self.addon))

    def process_sandbox(self):
        """Set an addon back to sandbox."""
        self.set_addon(status=amo.STATUS_NULL)
        self.set_files(amo.STATUS_DISABLED, self.version.files.all(),
                       hide_disabled_file=True)

        self.log_action(amo.LOG.REJECT_VERSION)
        self.notify_email('%s_to_sandbox' % self.review_type,
                          u'Mozilla Add-ons: %s %s Rejected')

        log.info(u'Making %s disabled' % (self.addon))
        log.info(u'Sending email for %s' % (self.addon))

    def process_preliminary(self):
        """Set an addon to preliminary."""
        if self.addon.is_premium():
            raise AssertionError('Premium add-ons cannot become preliminary.')

        changes = {'status': amo.STATUS_LITE}
        if (self.addon.status in (amo.STATUS_PUBLIC,
                                  amo.STATUS_LITE_AND_NOMINATED)):
            changes['highest_status'] = amo.STATUS_LITE

        template = '%s_to_preliminary' % self.review_type
        if (self.review_type == 'preliminary' and
            self.addon.status == amo.STATUS_LITE_AND_NOMINATED):
            template = 'nominated_to_nominated'

        self.set_addon(**changes)
        self.set_files(amo.STATUS_LITE, self.version.files.all(),
                       copy_to_mirror=True)

        self.log_action(amo.LOG.PRELIMINARY_VERSION)
        self.notify_email(template,
                          u'Mozilla Add-ons: %s %s Preliminary Reviewed')

        log.info(u'Making %s preliminary' % (self.addon))
        log.info(u'Sending email for %s' % (self.addon))

    def process_super_review(self):
        """Give an addon super review."""
        self.addon.update(admin_review=True)
        self.notify_email('author_super_review',
                          u'Mozilla Add-ons: %s %s flagged for Admin Review')
        self.send_super_mail()

    def process_comment(self):
        self.version.update(has_editor_comment=True)
        self.log_action(amo.LOG.COMMENT_VERSION)


class ReviewHelper:
    """
    A class that builds enough to render the form back to the user and
    process off to the correct handler.
    """
    def __init__(self, request=None, addon=None, version=None):
        self.handler = None
        self.required = {}
        self.addon = addon
        self.all_files = version.files.all()
        self.get_review_type(request, addon, version)
        self.actions = self.get_actions()

    def set_data(self, data):
        self.handler.set_data(data)

    def get_review_type(self, request, addon, version):
        if self.addon.type == amo.ADDON_WEBAPP:
            self.review_type = 'apps'
            self.handler = ReviewAddon(request, addon, version, 'pending')
        elif self.addon.status in NOMINATED_STATUSES:
            self.review_type = 'nominated'
            self.handler = ReviewAddon(request, addon, version, 'nominated')

        elif self.addon.status == amo.STATUS_UNREVIEWED:
            self.review_type = 'preliminary'
            self.handler = ReviewAddon(request, addon, version, 'preliminary')

        elif self.addon.status == amo.STATUS_LITE:
            self.review_type = 'preliminary'
            self.handler = ReviewFiles(request, addon, version, 'preliminary')
        else:
            self.review_type = 'pending'
            self.handler = ReviewFiles(request, addon, version, 'pending')

    def get_actions(self):
        if self.addon.type == amo.ADDON_WEBAPP:
            return self.get_app_actions()
        labels, details = self._review_actions()

        actions = SortedDict()
        if self.review_type != 'preliminary':
            actions['public'] = {'method': self.handler.process_public,
                                 'minimal': False,
                                 'label': _lazy('Push to public')}

        if not self.addon.is_premium():
            actions['prelim'] = {'method': self.handler.process_preliminary,
                                 'label': labels['prelim'],
                                 'minimal': False}

        actions['reject'] = {'method': self.handler.process_sandbox,
                             'label': _lazy('Reject'),
                             'minimal': False}
        actions['info'] = {'method': self.handler.request_information,
                           'label': _lazy('Request more information'),
                           'minimal': True}
        actions['super'] = {'method': self.handler.process_super_review,
                            'label': _lazy('Request super-review'),
                            'minimal': True}
        actions['comment'] = {'method': self.handler.process_comment,
                              'label': _lazy('Comment'),
                              'minimal': True}
        for k, v in actions.items():
            v['details'] = details.get(k)

        return actions

    def get_app_actions(self):
        actions = SortedDict()
        actions['public'] = {'method': self.handler.process_public,
                             'minimal': False,
                             'label': _lazy('Push to public'),
                             'details': _lazy(
                                'This will approve the sandboxed app so it '
                                'appears on the public side.')}
        actions['reject'] = {'method': self.handler.process_sandbox,
                             'label': _lazy('Reject'),
                             'minimal': False,
                             'details': _lazy(
                                'This will reject the app and remove it '
                                'from the review queue.')}
        actions['comment'] = {'method': self.handler.process_comment,
                              'label': _lazy('Comment'),
                              'minimal': True,
                              'details': _lazy(
                                    'Make a comment on this app.  The '
                                    'author won\'t be able to see this.')}
        return actions

    def _review_actions(self):
        labels = {'prelim': _lazy('Grant preliminary review')}
        details = {'prelim': _lazy('This will mark the files as '
                                   'premliminary reviewed.'),
                   'info': _lazy('Use this form to request more information '
                                 'from the author. They will receive an email '
                                 'and be able to answer here. You will be '
                                 'notified by email when they reply.'),
                   'super': _lazy('If you have concerns about this add-on\'s '
                                  'security, copyright issues, or other '
                                  'concerns that an administrator should look '
                                  'into, enter your comments in the area '
                                  'below. They will be sent to '
                                  'administrators, not the author.'),
                   'reject': _lazy('This will reject the add-on and remove '
                                   'it from the review queue.'),
                   'comment': _lazy('Make a comment on this version.  The '
                                    'author won\'t be able to see this.')}

        if self.addon.status == amo.STATUS_LITE:
            details['reject'] = _lazy('This will reject the files and remove '
                                      'them from the review queue.')

        if self.addon.status in (amo.STATUS_UNREVIEWED, amo.STATUS_NOMINATED):
            details['prelim'] = _lazy('This will mark the add-on as '
                                      'preliminarily reviewed. Future '
                                      'versions will undergo '
                                      'preliminary review.')
        elif self.addon.status == amo.STATUS_LITE:
            details['prelim'] = _lazy('This will mark the files as '
                                      'preliminarily reviewed. Future '
                                      'versions will undergo '
                                      'preliminary review.')
        elif self.addon.status == amo.STATUS_LITE_AND_NOMINATED:
            labels['prelim'] = _lazy('Retain preliminary review')
            details['prelim'] = _lazy('This will retain the add-on as '
                                      'preliminarily reviewed. Future '
                                      'versions will undergo preliminary '
                                      'review.')
        if self.review_type == 'pending':
            details['public'] = _lazy('This will approve a sandboxed version '
                                      'of a public add-on to appear on the '
                                      'public side.')
            details['reject'] = _lazy('This will reject a version of a public '
                                      'add-on and remove it from the queue.')
        else:
            details['public'] = _lazy('This will mark the add-on and its most '
                                      'recent version and files as public. '
                                      'Future versions will go into the '
                                      'sandbox until they are reviewed by an '
                                      'editor.')

        return labels, details

    def process(self):
        action = self.handler.data.get('action', '')
        if not action:
            raise NotImplementedError
        return self.actions[action]['method']()
