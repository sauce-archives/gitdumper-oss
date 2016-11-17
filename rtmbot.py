#!/usr/bin/env python

import sys
sys.dont_write_bytecode = True

import glob
import yaml
import os
import sys
import time
import logging
from argparse import ArgumentParser

from slackclient import SlackClient


def dbg(debug_string):
    if debug:
        logging.info(debug_string)


class RtmBot(object):
    def __init__(self, token):
        self.last_ping = 0
        self.token = token
        self.bot_plugins = []
        self.slack_client = None

    def connect(self):
        """Convenience method that creates Server instance"""
        logging.info("Connecting to Slack...")
        self.slack_client = SlackClient(self.token)
        self.slack_client.rtm_connect()
        logging.info("... connected.")

    def start(self):
        self.connect()
        self.load_plugins()
        while True:
            for reply in self.slack_client.rtm_read():
                self.input(reply)
            self.crons()
            self.execute_actions()
            self.autoping()
            time.sleep(.1)

    def autoping(self):
        # hardcode the interval to 3 seconds
        now = int(time.time())
        if now > self.last_ping + 3:
            self.slack_client.server.ping()
            self.last_ping = now

    def input(self, data):
        if "type" in data:
            function_name = "process_" + data["type"]
            dbg("got {}".format(function_name))
            for plugin in self.bot_plugins:
                plugin.register_jobs()
                plugin.do(function_name, data)

    def execute_actions(self):
        for plugin in self.bot_plugins:
            limiter = False
            for action in plugin.do_actions():
                if limiter is True:
                    time.sleep(.1)
                    limiter = False
                logging.info(self.slack_client.api_call(**action))
                limiter = True

    def crons(self):
        for plugin in self.bot_plugins:
            plugin.do_jobs()

    def load_plugins(self):
        for plugin in glob.glob(directory+'/plugins/*'):
            sys.path.insert(0, plugin)
            sys.path.insert(0, directory+'/plugins/')
        for plugin in glob.glob(directory+'/plugins/*.py') + glob.glob(directory+'/plugins/*/*.py'):
            name = plugin.split('/')[-1][:-3]
            self.bot_plugins.append(Plugin(name))


class Plugin(object):
    def __init__(self, name, plugin_config={}):
        self.name = name
        self.jobs = []
        self.module = __import__(name)
        self.register_jobs()
        self.outputs = []
        if name in config:
            logging.info("config found for: " + name)
            self.module.config = config[name]
        if 'setup' in dir(self.module):
            self.module.setup()

    def register_jobs(self):
        if 'crontable' in dir(self.module):
            for interval, function in self.module.crontable:
                self.jobs.append(Job(interval, eval("self.module."+function)))
            self.module.crontable = []
        else:
            self.module.crontable = []

    def do(self, function_name, data):
        if function_name in dir(self.module):
            # this makes the plugin fail with stack trace in debug mode
            if not debug:
                try:
                    eval("self.module."+function_name)(data)
                except:
                    dbg("problem in module {} {}".format(function_name, data))
            else:
                eval("self.module."+function_name)(data)
        if "catch_all" in dir(self.module):
            try:
                self.module.catch_all(data)
            except:
                dbg("problem in catch all")

    def do_jobs(self):
        for job in self.jobs:
            job.check()

    def do_actions(self):
        actions = []
        while True:
            if 'actions' in dir(self.module):
                if len(self.module.actions) > 0:
                    actions.append(self.module.actions.pop(0))
                else:
                    break
            else:
                self.module.actions = []
        return actions


class Job(object):
    def __init__(self, interval, function):
        self.function = function
        self.interval = interval
        self.lastrun = 0

    def __str__(self):
        return "{} {} {}".format(self.function, self.interval, self.lastrun)

    def __repr__(self):
        return self.__str__()

    def check(self):
        if self.lastrun + self.interval < time.time():
            if not debug:
                try:
                    self.function()
                except:
                    dbg("problem")
            else:
                self.function()
            self.lastrun = time.time()
            pass


class UnknownChannel(Exception):
    pass


def main_loop():
    logging_kwargs = dict(level=logging.INFO, format='%(asctime)s %(message)s')
    if "LOGFILE" in config:
        logging_kwargs['filename'] = config["LOGFILE"]
    logging.basicConfig(**logging_kwargs)
    logging.info(directory)
    try:
        bot.start()
    except KeyboardInterrupt:
        sys.exit(0)
    except:
        logging.exception('OOPS')


def parse_args():
    parser = ArgumentParser()
    parser.add_argument(
        '-c',
        '--config',
        help='Full path to config file.',
        metavar='path'
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    directory = os.path.dirname(sys.argv[0])
    if not directory.startswith('/'):
        directory = os.path.abspath(
            "{}/{}".format(os.getcwd(), directory))

    config = yaml.load(file(args.config or 'rtmbot.conf', 'r'))
    debug = config["DEBUG"]
    bot = RtmBot(config["SLACK_TOKEN"])
    site_plugins = []
    files_currently_downloading = []
    job_hash = {}

    if config.get("DAEMON"):
        import daemon
        with daemon.DaemonContext():
            main_loop()
    main_loop()
