import argparse
import configparser
import datetime
import json
import logging
import os
import platform
import time
from ast import literal_eval

import paho.mqtt.client as mqtt
import schedule
from pytimeparse.timeparse import timeparse

parser = argparse.ArgumentParser(
    "mqttutil",
    description="publish system information via mqtt",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument("-c", "--config", help="configuration file", type=str, default="etc/mqttutil.conf")
parser.add_argument("-v", "--verbose", help="increase output verbosity", action="count", default=0)

publish_options = parser.add_argument_group("publish")
publish_options.add_argument("--mqtt-host", help="hostname of mqtt broker", default="localhost", type=str)
publish_options.add_argument("--mqtt-port", help="port of mqtt broker", default=1883, type=int)
publish_options.add_argument(
    "--json", help="publish json dict instead of primitive datatypes", action="store_true", default=False
)
publish_options.add_argument("--outpath", help="path for local storage", default=None, type=str)

logger = logging.getLogger("mqttutil")


class Task:
    def __init__(
        self,
        mqtt_c: mqtt.Client,
        json: bool,
        topic: str,
        func: str,
        scheduling_interval: str,
        topic_prefix: str = f"{platform.node()}/mqttutil",
        requires: list[str] = [],
        qos: int = 0,
        test: bool = True,
        outpath: str | None = None,
        **kwargs,
    ):
        super().__init__()

        for imp in requires:
            if imp not in globals():
                logger.info("importing module %s", imp)
                globals()[imp] = __import__(imp)

        # text function
        self.func_str = func

        # set mqtt
        self.mqtt_c = mqtt_c
        self.json = json
        self.topic_prefix = topic_prefix
        self.topic_suffix = topic
        self.qos = qos

        self.jsonl_path: str | None
        if outpath:
            self.jsonl_path = os.path.join(outpath, f"{self.topic}.jsonl")
            jsonl_dir = os.path.dirname(self.jsonl_path)
            os.makedirs(jsonl_dir, exist_ok=True)
        else:
            self.jsonl_path = None

        if test:
            try:
                result = self._eval()
                self._publish(self.topic, result)
            except Exception as ex:
                logger.warning("testing %s failed: %s", self.topic, ex)

        # add to schedule
        scheduling_interval_s = timeparse(scheduling_interval)
        if scheduling_interval_s is None:
            raise RuntimeError("Couldn't parse scheduling interval '%s'", scheduling_interval)
        self.scheduling_interval_s = int(scheduling_interval_s)
        schedule.every(self.scheduling_interval_s).seconds.do(Task.run, self)

    def __repr__(self):
        return f"{self.__class__.__name__}('{self.func_str}')"

    @property
    def topic(self):
        if not self.topic_prefix:
            return self.topic_suffix
        elif self.topic_prefix.endswith("/"):
            return self.topic_prefix + self.topic_suffix
        else:
            return self.topic_prefix + "/" + self.topic_suffix

    def _eval(self):
        logger.debug("exec %s", self.func_str)
        result = eval(self.func_str)
        return result

    def _publish(self, pub_topic: str, result):
        # if json mode is set, publish json dict instead of primitive datatypes
        if self.json:
            result_dict: dict

            if isinstance(result, dict):
                result_dict = result
            elif isinstance(result, tuple) and hasattr(result, "_asdict"):
                result_dict = result._asdict()
            elif isinstance(result, tuple) or isinstance(result, list):
                result_dict = dict(enumerate(result))
            else:
                result_dict = {0: result}

            result_json = json.dumps(result_dict)
            logger.info("publish %s %s", pub_topic, result_json)
            self.mqtt_c.publish(pub_topic, result_json, qos=self.qos)

            if self.jsonl_path:
                with open(self.jsonl_path, "a") as jsonl_file:
                    jsonl_dict = {"_time": datetime.datetime.now().astimezone(), **result_dict}
                    jsonl_file.write(json.dumps(jsonl_dict, default=str) + "\n")

            return

        # don't publish Nones
        if result is None:
            return

        # publish primitive data directly
        elif type(result) in [int, float, str]:
            logger.info("publish %s %s", pub_topic, result)
            self.mqtt_c.publish(pub_topic, result, qos=self.qos)

        # expand dict by keys
        elif isinstance(result, dict):
            for k, v in result.items():
                self._publish(f"{pub_topic}/{k}", v)

        # iterate list (via dict conversion)
        elif isinstance(result, list):
            self._publish(pub_topic, dict(enumerate(result)))

        elif isinstance(result, tuple):
            # recurse as dict for namedtuple
            if hasattr(result, "_asdict") and hasattr(result, "_fields"):
                self._publish(pub_topic, result._asdict())
            # iterate regular tuple (via dict conversion)
            else:
                self._publish(pub_topic, dict(enumerate(result)))

        # print info on unknown result types
        else:
            logger.warning("type %s is not supported. (%s)", type(result), pub_topic)

    def run(self):
        try:
            result = self._eval()
            self._publish(self.topic, result)
        except Exception as ex:
            logger.warning("Task [%s] failed: ", self.topic_suffix)
            logger.exception(ex)


if __name__ == "__main__":
    args = parser.parse_args()

    # setup logger
    logger_level = max(0, logging.WARN - (args.verbose * 10))
    logging.basicConfig(level=logger_level)

    # get mqtt config
    mqtt_c = mqtt.Client(client_id=f"{platform.node()}-mqttutil", clean_session=False)
    mqtt_c.connect(args.mqtt_host, args.mqtt_port)
    mqtt_c.loop_start()

    # parse config
    config = configparser.ConfigParser()
    config.read(args.config)

    # tasks
    tasks: list[Task] = []

    # look for known sections
    for topic in config.sections():
        var = {k: literal_eval(v) for k, v in config.items(topic)}
        try:
            tasks.append(Task(mqtt_c, args.json, topic, outpath=args.outpath, **var))
        except Exception as e:
            logger.warning("Task '%s' cannot be created:", topic)
            logger.exception(e)

    mqtt_c._keepalive = min([t.scheduling_interval_s for t in tasks])
    mqtt_c.reconnect()

    if not tasks:
        logger.critical("No valid tasks specified, exiting.")
        exit(1)

    running = True
    while running:
        time.sleep(1)
        schedule.run_pending()
