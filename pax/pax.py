import logging
import inspect
import argparse
import configparser
import glob
#import itertools

from io import StringIO
import os
from pluginbase import PluginBase

from pax import units


def evaluate_configuration(config):
    evaled_config = {}
    for key, value in config.items():
        # Eval value with globals = everything from 'units' module...
        evaled_config[key] = eval(value, {
            name: getattr(units, name)
            for name in dir(units)
        })
    return evaled_config


def instantiate(name, plugin_source, config_values, log=logging):
    """take class name and build class from it"""
    name_module, name_class = name.split('.')
    try:
        plugin_module = plugin_source.load_plugin(name_module)
    except ImportError as e:
        log.fatal("Failed to load plugin %s" % name_module)
        log.exception(e)
        raise

    if config_values.has_section(name):
        this_config = config_values[name]
    else:
        this_config = config_values['DEFAULT']

    this_config = evaluate_configuration(this_config)

    return getattr(plugin_module, name_class)(this_config)


def get_my_dir():
    """Find location of this file, then my_dir is its directory"""
    absolute_path = os.path.abspath(inspect.getfile(inspect.currentframe()))
    my_dir = os.path.dirname(absolute_path)
    return my_dir


def get_configuration(config_overload=""):
    my_dir = get_my_dir()

    config = configparser.ConfigParser(inline_comment_prefixes='#',
                                       strict=True)

    # Allow for case-sensitive configuration keys
    config.optionxform = str

    # Load the default configuration
    config.read(os.path.join(my_dir, 'default.ini'))
    config.read_string(config_overload)
    return config


def get_plugin_source(config, log=logging):
    # Setup plugins (which involves finding the plugin directory.
    plugin_base = PluginBase(package='pax.plugins')
    searchpath = ['./plugins'] + config['DEFAULT']['plugin_paths'].split()
    # Find the absolute path, then director, then find plugin directory
    searchpath += [os.path.join(get_my_dir(), '..', 'plugins')]
    # Want to look in all subdirectories of plugin directories
    for entry in searchpath:
        searchpath.extend(glob.glob(os.path.join(entry, '*/')))

    searchpath = [path for path in searchpath if '__pycache__' not in path]

    log.debug("Search path for plugins is %s" % str(searchpath))
    plugin_source = plugin_base.make_plugin_source(searchpath=searchpath)
    log.info("Found the following plugins:")
    for plugin_name in plugin_source.list_plugins():
        log.info("\tFound %s" % plugin_name)

    return plugin_source


def get_actions(config, input, list_of_names):
    config = evaluate_configuration(config['pax'])

    input = config[input]
    if not isinstance(input, str):
        raise ValueError("Input class name must be string")

    actions = []

    for name in list_of_names:
        value = config[name]

        if not isinstance(value, (str, list)):
            raise ValueError("Misformatted ini file on key %s" % name)

        if not isinstance(value, list):
            value = [value]

        actions += value

    return input, actions


def processor(config_overload=""):
    # Load configuration
    config = get_configuration(config_overload)

    input, actions = get_actions(config,
                                 'input',
                                 ['dsp', 'transform', 'my_postprocessing', 'output'])

    # Deal with command line arguments for the logging level
    parser = argparse.ArgumentParser(description="Process XENON1T data")
    parser.add_argument('--log', default='INFO', help="Set log level")
    # Suggestion: provide more detailed input control?
    # parser.add_argument('--single',
    #                     type=int,
    #                     help="Process a single event.")
    # parser.add_argument('--start',
    #                     type=int,
    #                     default=0,
    #                     help="First event to process.")
    # parser.add_argument('--stop',
    #                     type=int,
    #                     help="Last event to process.")
    # parser.add_argument('--skip',
    #                     type=int,
    #                     default=0,
    #                     help="Skip this many events after processing an event (useful for subset processing).")
    parser.add_argument('-n',
                        type=int,
                        help="Stop after this number of events has been processed.")
    args = parser.parse_args()

    # Setup logging
    numeric_level = getattr(logging, args.log.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError('Invalid log level: %s' % args.log.upper())

    logging.basicConfig(level=numeric_level,
                        format='%(name)s L%(lineno)s - %(levelname)s %(message)s')
    log = logging.getLogger('processor')

    # Print settings to log
    string_file = StringIO()
    config.write(string_file)
    log.debug("Dumping INI file")
    for line in string_file.getvalue().split('\n'):
        log.debug(line)

    # Gather information about plugins
    plugin_source = get_plugin_source(config, log)

    input = instantiate(input, plugin_source, config, log)
    actions = [instantiate(x, plugin_source, config, log) for x in actions]

    # Suggestion: provide more detailed input control?
    # for i in itertools.count(args.start, 1 + args.skip):
    #     try:
    #         event = input.get_event(i)
    #     except EventDoesNotExistError:
    #         self.log.warning("Event %s does not exist! Continuing processing...")
    #     except InputPluginError as e:
    #         raise RuntimeError("Input plugin error: %s" % e.strerror)
    #     # Do actions per plugin

    # This is the *actual* event loop
    for i, event in enumerate(input.get_events()):
        if args.n:
            if i >= args.n:
                break

        log.info("Event %d" % i)
        # Can we catch plugin errors here and move to the next event?
        # That prevents the entire processing from crashing on rare plugin errors.
        for j, block in enumerate(actions):
            log.debug("Step %d with %s", j, block.__class__.__name__)
            event = block.process_event(event)
    else:
        log.info("Finished event loop.")