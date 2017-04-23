from __future__ import absolute_import, unicode_literals

from collections import OrderedDict
from contextlib import contextmanager
from os.path import normpath
from pprint import pformat

from django import http
from django.conf.urls import url
from django.db.models.query import QuerySet, RawQuerySet
from django.template import RequestContext, Template
from django.test.signals import template_rendered
from django.test.utils import instrumented_test_render
from django.utils import six
from django.utils.encoding import force_text
from django.utils.translation import ugettext_lazy as _

from debug_toolbar.panels import Panel
from debug_toolbar.panels.sql.tracking import SQLQueryTriggered, recording
from debug_toolbar.panels.templates import views

# Monkey-patch to enable the template_rendered signal. The receiver returns
# immediately when the panel is disabled to keep the overhead small.

# Code taken and adapted from Simon Willison and Django Snippets:
# https://www.djangosnippets.org/snippets/766/

if Template._render != instrumented_test_render:
    Template.original_render = Template._render
    Template._render = instrumented_test_render


# Monkey-patch to store items added by template context processors. The
# overhead is sufficiently small to justify enabling it unconditionally.

@contextmanager
def _request_context_bind_template(self, template):
    if self.template is not None:
        raise RuntimeError("Context is already bound to a template")

    self.template = template
    # Set context processors according to the template engine's settings.
    processors = (template.engine.template_context_processors +
                  self._processors)
    self.context_processors = OrderedDict()
    updates = {}
    for processor in processors:
        name = '%s.%s' % (processor.__module__, processor.__name__)
        context = processor(self.request)
        self.context_processors[name] = context
        updates.update(context)
    self.dicts[self._processors_index] = updates

    try:
        yield
    finally:
        self.template = None
        # Unset context processors.
        self.dicts[self._processors_index] = {}


RequestContext.bind_template = _request_context_bind_template


class TemplatesPanel(Panel):
    """
    A panel that lists all templates used during processing of a response.
    """
    def __init__(self, *args, **kwargs):
        super(TemplatesPanel, self).__init__(*args, **kwargs)
        self.templates = []
        # Refs GitHub issue #910
        # Holds a collection of unique contexts, keyed by the id()
        # of them, with the value holding a list of  `pformat` output
        # of the original layers. See _store_template_info.
        self.seen_contexts = {}


    def _store_template_info(self, sender, **kwargs):
        template, context = kwargs['template'], kwargs['context']

        # Skip templates that we are generating through the debug toolbar.
        if (isinstance(template.name, six.string_types) and
                template.name.startswith('debug_toolbar/')):
            return

        context_list = []
        for context_layer in context.dicts:
            if hasattr(context_layer, 'items'):
                # Refs #910
                # The same dictionary may be passed around a lot, so if we've
                # seen it before, just re-use the previous output.
                # We use the id *and* the keys because for some reason
                # just using the id doesn't work correctly (for example,
                # it misses a whole lot of data for select_option.html?)
                context_layer_id = (id(context_layer), tuple(sorted(context_layer.keys())))
                if context_layer_id not in self.seen_contexts:
                    temp_layer = {}
                    for key, value in context_layer.items():
                        # Replace any request elements - they have a large
                        # unicode representation and the request data is
                        # already made available from the Request panel.
                        if isinstance(value, http.HttpRequest):
                            temp_layer[key] = '<<request>>'
                        # Replace the debugging sql_queries element. The SQL
                        # data is already made available from the SQL panel.
                        elif key == 'sql_queries' and isinstance(value, list):
                            temp_layer[key] = '<<sql_queries>>'
                        # Replace LANGUAGES, which is available in i18n context processor
                        elif key == 'LANGUAGES' and isinstance(value, tuple):
                            temp_layer[key] = '<<languages>>'
                        # QuerySet would trigger the database: user can run the query from SQL Panel
                        elif isinstance(value, (QuerySet, RawQuerySet)):
                            model_name = "%s.%s" % (
                                value.model._meta.app_label, value.model.__name__)
                            temp_layer[key] = '<<%s of %s>>' % (
                                value.__class__.__name__.lower(), model_name)
                        else:
                            try:
                                recording(False)
                                force_text(value)  # this MAY trigger a db query
                            except SQLQueryTriggered:
                                temp_layer[key] = '<<triggers database query>>'
                            except UnicodeEncodeError:
                                temp_layer[key] = '<<unicode encode error>>'
                            except Exception:
                                temp_layer[key] = '<<unhandled exception>>'
                            else:
                                temp_layer[key] = value
                            finally:
                                recording(True)
                    try:
                        prettified_layer = force_text(pformat(temp_layer))
                    except UnicodeEncodeError:
                        pass
                    else:
                        self.seen_contexts[context_layer_id] = prettified_layer
                context_list.append(self.seen_contexts[context_layer_id])

        kwargs['context'] = context_list
        kwargs['context_processors'] = getattr(context, 'context_processors', None)
        self.templates.append(kwargs)

    # Implement the Panel API

    nav_title = _("Templates")

    @property
    def title(self):
        num_templates = len(self.templates)
        return _("Templates (%(num_templates)s rendered)") % {'num_templates': num_templates}

    @property
    def nav_subtitle(self):
        if self.templates:
            return self.templates[0]['template'].name
        return ''

    template = 'debug_toolbar/panels/templates.html'

    @classmethod
    def get_urls(cls):
        return [
            url(r'^template_source/$', views.template_source, name='template_source'),
        ]

    def enable_instrumentation(self):
        template_rendered.connect(self._store_template_info)

    def disable_instrumentation(self):
        template_rendered.disconnect(self._store_template_info)

    def generate_stats(self, request, response):
        template_context = []
        for template_data in self.templates:
            info = {}
            # Clean up some info about templates
            template = template_data.get('template', None)
            if hasattr(template, 'origin') and template.origin and template.origin.name:
                template.origin_name = template.origin.name
            else:
                template.origin_name = _('No origin')
            info['template'] = template
            # Clean up context for better readability
            if self.toolbar.config['SHOW_TEMPLATE_CONTEXT']:
                context_list = template_data.get('context', [])
                info['context'] = '\n'.join(context_list)
            template_context.append(info)

        # Fetch context_processors/template_dirs from any template
        if self.templates:
            context_processors = self.templates[0]['context_processors']
            template = self.templates[0]['template']
            # django templates have the 'engine' attribute, while jinja templates use 'backend'
            engine_backend = getattr(template, 'engine', None) or getattr(template, 'backend')
            template_dirs = engine_backend.dirs
        else:
            context_processors = None
            template_dirs = []

        self.record_stats({
            'templates': template_context,
            'template_dirs': [normpath(x) for x in template_dirs],
            'context_processors': context_processors,
        })
