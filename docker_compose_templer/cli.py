import argparse
import io
import os
import sys
import traceback
from ast import literal_eval
from copy import deepcopy
from distutils.util import strtobool

import jinja2
import pyinotify
import ruamel.yaml as yaml

from docker_compose_templer import __version__
from docker_compose_templer import jinja_filter

try:
    from hashlib import sha1
except ImportError:
    from sha import sha as sha1


class Utils:

    @staticmethod
    def merge_dicts(x, y):
        """Recursively merges two dicts.

        When keys exist in both the value of 'y' is used.

        Args:
            x (dict): 
            y (dict): Second dict

        Returns:
            dict: 

        """
        if x is None and y is None:
            return dict()
        if x is None:
            return y
        if y is None:
            return x

        merged = dict(x, **y)
        xkeys = x.keys()

        for key in xkeys:
            if type(x[key]) is dict and key in y:
                merged[key] = Utils.merge_dicts(x[key], y[key])
        return merged

    @staticmethod
    def load_yaml(string, Loader=yaml.CSafeLoader):
        """Parse a YAML string and produce the corresponding Python object.

        Args:
            string (str): The input string to be parsed
            Loader (yaml.Loader): Loader to use for parsing

        Returns:
            dict: The parsed YAML

        Raises:
            yaml.YAMLError: If the YAML string is malformed
        """
        try:
            Log.debug("Parsing YAML...")
            return yaml.load(string, Loader=Loader) or dict()
        except yaml.YAMLError as e:
            if hasattr(e, 'problem_mark'):
                raise yaml.YAMLError(
                    "YAML parsing error:\n{0}\n  {1}\n  {2}".format(e.context_mark, e.problem, e.problem_mark))
            else:
                raise yaml.YAMLError("YAML parsing error: {0}".format(str(e)))
        except Exception as e:
            raise

    @staticmethod
    def evaluate_string(string):
        """Evaluates a string containing a Python value.

        Args:
            string(str): A Python value represented as a string

        Returns:
            str, int, float, bool, list or dict: The value of the evaluated string
        """
        try:
            # evaluate to int, float, list, dict
            return literal_eval(string.strip())
        except Exception as e:
            try:
                # evaluate bool from different variations
                return bool(strtobool(string.strip()))
            except Exception as e:
                # string cannot be evaluated -> return string
                return string

    @staticmethod
    def format_error(heading, **kwargs):
        """Formats an error for pretty cli output

        Args:
            heading (str): Error message heading
            kwargs (dict): Key value pairs to be printed aligned

        Returns:
            str: THe formatted error message

        """
        indent = max([len(x) for x in kwargs.keys()]) + 2
        row_format = '{:>' + str(indent) + '}: {}'
        row_format_continuation = ' ' * (indent + 6) + '{}'

        formatted_error = [heading]
        for k, v in kwargs.items():
            lines = v.splitlines()
            formatted_error.append(row_format.format(
                k.replace('_', ' ').title(), lines[0]))
            for i in range(1, len(lines)):
                formatted_error.append(row_format_continuation.format(lines[i]))

        return '\n'.join(formatted_error)


class JinjaRenderer(object):
    omit_placeholder = '__omit_place_holder__%s' % sha1(os.urandom(64)).hexdigest()

    def __init__(self):
        self.env = jinja2.Environment(
            lstrip_blocks=True,
            trim_blocks=True,
            undefined=jinja2.StrictUndefined
        )

        # Register additional filters
        self.env.filters = Utils.merge_dicts(self.env.filters, jinja_filter.filters)

    class Omit(object):
        pass

    def render_string(self, template_string, context):
        # add omit variable to context
        context['omit'] = JinjaRenderer.omit_placeholder

        try:
            return self.env.from_string(template_string).render(context)
        except jinja_filter.MandatoryError as e:
            raise e
        except jinja2.UndefinedError as e:
            raise jinja2.exceptions.UndefinedError('Variable {0}'.format(str(e.message)))
        except jinja2.TemplateError as e:
            raise jinja2.exceptions.TemplateError('Template error: {0}'.format(str(e.message)))
        except Exception as e:
            raise e

    def render_dict_and_add_to_context(self, the_dict, context):
        new_context = deepcopy(context)
        for k, v in the_dict.items():
            processed_value = self._render_dict(v, new_context)
            if type(processed_value) is not JinjaRenderer.Omit:
                new_context = Utils.merge_dicts(new_context, {k: processed_value})
        return new_context

    def _render_dict(self, value, context):
        if value is None:
            return None

        # str
        elif type(value) is str:
            rendered_value = self.render_string(value, context)
            if rendered_value == value:
                return value
            else:
                if rendered_value.find(JinjaRenderer.omit_placeholder) != -1:
                    return JinjaRenderer.Omit()
                else:
                    return Utils.evaluate_string(rendered_value)

        # lists
        elif type(value) is list:
            new_list = []
            for li in value:
                processed_item = self._render_dict(li, context)
                if type(processed_item) is not JinjaRenderer.Omit:
                    new_list.append(processed_item)
            return new_list

        # dicts
        elif type(value) is dict:
            new_dict = dict()
            for k, v in value.items():
                processed_value = self._render_dict(v, context)
                if type(processed_value) is not JinjaRenderer.Omit:
                    new_dict[k] = processed_value
            return new_dict

        # other types
        else:
            return value

    def remove_omit_from_dict(self, value):
        if value is None:
            return None

        elif type(value) is str:
            if value.find(JinjaRenderer.omit_placeholder) != -1:
                return JinjaRenderer.Omit()
            else:
                return value

        # lists
        elif type(value) is yaml.comments.CommentedSeq or type(value) is list:
            vlen = len(value)
            for i in range(vlen - 1, -1, -1):
                processed_item = self.remove_omit_from_dict(value[i])
                if type(processed_item) is JinjaRenderer.Omit:
                    del value[i]
                    i -= 1
            return value

        # dicts
        elif type(value) is yaml.comments.CommentedMap or type(value) is dict:
            for key in list(value.keys()):
                processed_value = self.remove_omit_from_dict(value[key])
                if type(processed_value) is JinjaRenderer.Omit:
                    del value[key]
            return value

        else:
            return value


class Log(object):
    """Stupid logger that writes messages to stdout or stderr accordingly"""

    ERROR = 30
    INFO = 20
    DEBUG = 10
    level = ERROR

    @staticmethod
    def debug(msg):
        if Log.level <= 10:
            sys.stdout.write(msg + "\n")

    @staticmethod
    def info(msg):
        if Log.level <= 20:
            sys.stdout.write(msg + "\n")

    @staticmethod
    def error(msg):
        sys.stderr.write(msg + "\n")


class ContextChainElement(object):

    def __init__(self, source, prev=None):
        self.prev = prev
        self.next = None
        self.source = source
        if type(source) == File:
            self.source.add_on_change_callback(self.execute_on_change)

        self.jr = JinjaRenderer()
        self.cache = None

    def get_context(self):
        if self.cache:
            return self.cache
        else:
            parent_context = self.prev.get_context() if self.prev else dict()
            if type(self.source) == ContextFile:
                file_content = self.source.read()
                try:
                    self.cache = self.jr.render_dict_and_add_to_context(
                        Utils.load_yaml(file_content),
                        parent_context
                    )
                except Exception as e:
                    raise Exception(Utils.format_error(
                        "Error loading variables from file",
                        description=str(e),
                        path=self.source.path
                    ))
            elif type(self.source) == dict:
                try:
                    self.cache = self.jr.render_dict_and_add_to_context(
                        self.source['data'],
                        parent_context
                    )
                except Exception as e:
                    raise Exception(Utils.format_error(
                        "Error loading variables",
                        description=str(e),
                        file_path=self.source['path']
                    ))
                

            return self.cache

    def invalidate_cache(self):
        self.cache = None

    def execute_on_change(self):
        raise NotImplementedError()


class ContextChain(object):

    def __init__(self):
        self.chain_elements = []

    def add_context(self, context, origin_path):
        if context:
            tail = self.chain_elements[-1] if self.chain_elements else None
            elm = ContextChainElement(
                source={'path': origin_path, 'data': context},
                prev=tail
            )
            self.chain_elements.append(elm)
            if tail:
                tail.next = elm

    def add_files(self, files, relative_path):
        for path in files:
            if not os.path.isabs(path):
                path = os.path.join(relative_path, path)
            tail = self.chain_elements[-1] if self.chain_elements else None
            elm = ContextChainElement(
                source=ContextFile(path),
                prev=tail
            )
            self.chain_elements.append(elm)
            if tail:
                tail.next = elm

    def get_context(self):
        return self.chain_elements[-1].get_context()


class File(object):
    def __init__(self, path):
        self.path = path
        self.on_change_callbacks = []

    def get_path(self):
        return self.path

    def exists(self):
        return os.path.exists(self.get_path())

    def read(self):
        path = self.get_path()
        if not self.exists():
            raise IOError(Utils.format_error(
                "Error reading file",
                description="File does not exist",
                path=path
            ))
        if not os.path.isfile(path):
            raise IOError(Utils.format_error(
                "Error reading file",
                description="Is not a file",
                path=path
            ))
        Log.debug("Loading file '{0}'...".format(path))
        with io.open(path, 'r', encoding='utf8') as f:
            file_content = f.read()

        return file_content

    def write(self, content, path, force_overwrite=False):
        """Writes the given content into the file

        Args:
            content (str): Content to write into the file

        Raises:
            IOError: If desired output file exists or is not a file
        """
        if os.path.exists(path):
            if os.path.isfile(path):
                if not force_overwrite:
                    raise IOError(Utils.format_error(
                        "Error writing file",
                        description="Destination already exists. Use '-f' flag to overwrite the file",
                        path=path
                    ))
            else:
                raise IOError(Utils.format_error(
                    "Error writing file",
                    description="Destination exists and is not a file",
                    path=path
                ))
        else:
            # create dir
            if os.path.dirname(self.path):
                os.makedirs(os.path.dirname(self.path), exist_ok=True)

        # write content to file
        Log.debug("Writing file '{0}'...".format(path))
        with io.open(path, 'w', encoding='utf8') as f:
            f.write(content)

    def add_on_change_callback(self, callback):
        self.on_change_callbacks.append(callback)

    def execute_on_change(self):
        for a in self.on_change_callbacks:
            a()


class ContextFile(File):

    def __init__(self, path):
        super().__init__(path)


class DefinitionFile(File):

    def __init__(self, path, force_overwrite=True):
        super().__init__(path)
        self.force_overwrite = force_overwrite

        self.jr = JinjaRenderer()
        self.templates = []

    def parse(self):
        self.templates = []

        file_content = self.read()
        try:
            file_content = Utils.load_yaml(file_content)
        except Exception as e:
            raise Exception(Utils.format_error(
                DefinitionFile._error_loading_msg,
                description=str(e),
                path=self.path
            ))

        if 'templates' not in file_content:
            self._raise_value_error("Missing 'templates' definition")

        for t in file_content['templates']:
            template_options = self._parse_common_options(t, True)

            # load local variables
            tcc = ContextChain()
            tcc.add_files(file_content['include_vars'], os.path.dirname(self.path))
            tcc.add_context(file_content['vars'], self.path)
            tcc.add_files(template_options['include_vars'], os.path.dirname(self.path))
            tcc.add_context(template_options['vars'], self.path)

            if 'src' in t:
                if type(t['src']) is str:
                    template_options['src'] = t['src']
                else:
                    self._raise_value_error("Value of 'src' must be of type string")
            else:
                self._raise_value_error("Missing key 'src' in template definition")

            if 'dest' in t:
                if type(t['dest']) is str:
                    template_options['dest'] = t['dest']
                else:
                    self._raise_value_error("Value of 'dest' must be of type string")
            else:
                self._raise_value_error("Missing key 'dest' in template definition")

            self.templates.append(
                TemplateFile(
                    src=template_options['src'],
                    dest=template_options['dest'],
                    relative_path=os.path.dirname(self.path),
                    context=tcc,
                    force_overwrite=self.force_overwrite,
                )
            )

    def _parse_common_options(self, options, set_defaults):
        processed_options = dict()

        if 'vars' in options:
            if type(options['vars']) is dict:
                processed_options['vars'] = options['vars']
            else:
                self._raise_value_error("Value of 'vars' must be of type dict")
        elif set_defaults:
            processed_options['vars'] = dict()

        if 'include_vars' in options:
            if type(options['include_vars']) is list:
                processed_options['include_vars'] = options['include_vars']
            elif type(options['include_vars']) is str:
                processed_options['include_vars'] = [options['include_vars']]
            else:
                self._raise_value_error("Value of 'include_vars' must be of type list or string")
        elif set_defaults:
            processed_options['include_vars'] = []

        return processed_options

    def get_template_files(self):
        return self.templates

    def render_templates(self):
        if self.templates:
            failed_renders = []
            for t in self.templates:
                try:
                    t.render()
                except Exception as e:
                    failed_renders.append(t)
                    if Log.level <= 10:
                        Log.error(traceback.format_exc())
                    else:
                        Log.error(str(e))

            if len(failed_renders) > 0:
                Log.error("Some renders failed:")
                for fr in failed_renders:
                    Log.error("    " + fr.get_path())
                return False

            return True

    def _raise_value_error(self, description):
        raise ValueError(Utils.format_error(
            "Error loading options from definition file",
            description=description,
            path=self.definition_file.path
        ))


class TemplateFile(File):
    """ Represents a template file to be rendered with jinja2

    Args:
        src (str): Path to template file
        dest (str): Path for rendered file
        context (dict): Jinja2 context
        force_overwrite (bool): Overwrite existing file

    """

    def __init__(self, src, dest, relative_path, context, force_overwrite=False):
        super().__init__(src)
        self.dest = dest
        self.relative_path = relative_path
        self.context = context
        self.force_overwrite = force_overwrite

        self.jr = JinjaRenderer()

    def get_path(self):
        return self._create_path(self.path)

    def _create_path(self, path):
        path = self.jr.render_string(path, self.context.get_context())
        if os.path.isabs(path):
            return path
        else:
            return os.path.join(self.relative_path, path)

    def render(self):
        """Renders the template file with jinja2"""

        file_content = self.read()
        path = self.get_path()

        try:
            Log.debug("Rendering template file...")
            rendered_file_content = self.jr.render_string(file_content, self.context.get_context())
        except Exception as e:
            raise Exception(Utils.format_error(
                "Error while rendering template",
                description=str(e),
                path=path
            ))

        # remove values containing an omit placeholder
        try:
            processed_content = yaml.dump(
                self.jr.remove_omit_from_dict(
                    Utils.load_yaml(rendered_file_content, Loader=yaml.RoundTripLoader)
                ),
                indent=2,
                block_seq_indent=2,
                allow_unicode=True,
                default_flow_style=False,
                Dumper=yaml.RoundTripDumper
            )
        except Exception as e:
            raise Exception(Utils.format_error(
                "Error while rendering template",
                description=str(e),
                path=path
            )).with_traceback(e.__traceback__)

        # Write rendered file
        dest_path = self._create_path(self.dest)
        self.write(
            content=rendered_file_content,
            path=dest_path,
            force_overwrite=self.force_overwrite
        )
        Log.info("Created file '{0}'".format(dest_path))


class AutoRenderer(object):

    def __init__(self, definition_files):
        self.definition_files = definition_files

    class RenderHandler(pyinotify.ProcessEvent):

        def __init__(self, template_file):
            self.template_file = template_file

        def process_IN_CREATE(self, event):
            self.render()

        def process_IN_MODIFY(self, event):
            self.render()

        def render(self):
            try:
                self.template_file.render()
            except Exception as e:
                Log.error(str(e))

    def start(self):
        import asyncore

        Log.info("Starting Auto Renderer...")
        mask = pyinotify.IN_CREATE | pyinotify.IN_MODIFY
        notifiers = []
        for df in self.definition_files:
            df.parse()
            for template in df.get_template_files():
                handler = self.RenderHandler(template)
                wm = pyinotify.WatchManager()
                notifiers.append(pyinotify.AsyncNotifier(wm, handler))
                wm.add_watch(template.path, mask)
                for cf in template.context.context_files:
                    wm.add_watch(cf.path, mask)

        notifiers[0].stop()
        del notifiers[0]
        asyncore.loop()


def cli():
    """ CLI entry point """
    # parsing arguments
    parser = argparse.ArgumentParser(
        prog='docker_compose_templer',
        description='Render Docker Compose file templates with the power of Jinja2',
        add_help=False)
    parser.add_argument('-a', '--auto-render', dest='auto_render',
                        action='store_true', default=False, help="Automatically render templates when a file changed")
    parser.add_argument('-f', '--force', dest='force_overwrite',
                        action='store_true', default=False, help="Overwrite existing files")
    parser.add_argument("-h", "--help", action="help",
                        help="Show this help message and exit")
    parser.add_argument('-v', '--verbose', dest='verbose', action='count',
                        default=0, help="Enable verbose mode (-vv for debug mode)")
    parser.add_argument('--version', action='version', version='Templer {0}, Jinja2 {1}'.format(
        __version__, jinja2.__version__), help="Prints the program version and quits")
    parser.add_argument('definition_files', nargs='+',
                        help="File that defines what to do.")
    args = parser.parse_args(sys.argv[1:])

    # initialize dumb logger
    levels = [Log.ERROR, Log.INFO, Log.DEBUG]
    Log.level = levels[min(len(levels) - 1, args.verbose)]

    try:
        definition_files = [
            DefinitionFile(
                path=path,
                force_overwrite=args.force_overwrite,
            ) for path in args.definition_files
        ]
        for df in definition_files:
            df.parse()

        if args.auto_render:
            ar = AutoRenderer(definition_files)
            exit(ar.start())

        else:
            render_failed = False
            for df in definition_files:
                if not df.render_templates():
                    render_failed = True

            if render_failed:
                exit(1)

    except Exception as e:
        # catch errors and print to stderr
        if args.verbose >= 2:
            Log.error(traceback.format_exc())
        else:
            Log.error(str(e))
        exit(1)

    exit(0)