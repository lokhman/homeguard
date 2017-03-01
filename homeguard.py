#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2015 Alexander Lokhman <alex.lokhman@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os
import sys
import time
import logging
import smtplib
import dropbox
import requests
import threading

import RPi.GPIO as GPIO

from picamera import PiCamera
from requests.packages import urllib3
from datetime import datetime, timedelta
from ConfigParser import SafeConfigParser, NoOptionError
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage


__version__ = "1.0"
__author__ = "Alexander Lokhman"
__email__ = "alex.lokhman@gmail.com"
__basename__ = os.path.splitext(__file__)[0]


class StreamLogger(object):
    def __init__(self, logger, stream, level=logging.INFO):
        self.logger = logger
        self.stream = stream
        self.level = level

    def write(self, buf):
        for line in buf.rstrip().splitlines():
            self.logger.log(self.level, line.strip())
        self.stream.write(buf)
        self.stream.flush()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    filename=__basename__ + ".log",
    filemode="a",
)

sys.stdout = StreamLogger(logging.getLogger("STDOUT"), sys.stdout)
sys.stderr = StreamLogger(logging.getLogger("STDERR"), sys.stderr, logging.ERROR)


class ConfigParser(SafeConfigParser):
    DEFAULT_SECTION = "default"

    def getlist(self, section, option, sep=","):
        return map(str.strip, self.get(section, option).split(sep))

config = ConfigParser()
config.read(__basename__ + ".ini")


def async(callback, *args, **kwargs):
    t = threading.Thread(target=callback, args=args, kwargs=kwargs)
    t.setDaemon(True)
    t.start()


class DevNull(object):
    def __getattr__(self, item):
        return DevNull()

    def __call__(self, *args, **kwargs):
        pass

_DEFAULT_ARG = object()


class Telegram(object):
    def __init__(self, token, chats):
        assert bool(chats)

        self._token = token
        self._chats = dict(u.split(":") for u in chats)

    def _get_url(self, action):
        return "https://api.telegram.org/bot%s/%s" % (self._token, action)

    def _post(self, endpoint, data, log_msg, **kwargs):
        req = requests.post(self._get_url(endpoint), data, **kwargs)
        if req.status_code == requests.codes.ok:
            print "> SUCCESS: %s" % log_msg
        else:
            print "> ERROR: %s" % log_msg
            sys.stderr.write(req.text)
        return req

    def send_message(self, text):
        for chat_id, name in self._chats.iteritems():
            self._post("sendMessage", {
                "chat_id": chat_id,
                "text": text.format(name),
            }, "Sending message to Telegram %s" % chat_id)
            time.sleep(1.0)

    def send_photo(self, path, caption=""):
        chat_ids = self._chats.keys()
        with open(path, "rb") as f:
            req = self._post("sendPhoto", {
                "chat_id": chat_ids[0],
                "caption": caption,
            }, "Sending photo to Telegram %s" % chat_ids[0], files={"photo": f})

        try:
            file_id = req.json()["result"]["photo"][-1]["file_id"]
        except AttributeError:
            return

        for chat_id in chat_ids[1:]:
            self._post("sendPhoto", {
                "chat_id": chat_id,
                "caption": caption,
                "photo": file_id,
            }, "Sending photo copy to Telegram %s" % chat_id)


class Dropbox(object):
    def __init__(self, token):
        self._client = dropbox.client.DropboxClient(token)

    def put_file(self, path):
        with open(path, "rb") as f:
            try:
                rel_path = "/".join(path.split("/")[-3:])
                self._client.put_file(rel_path, f)
                print "> SUCCESS: Photo was saved to Dropbox at %s" % rel_path
            except dropbox.rest.ErrorResponse as e:
                print "> ERROR: Photo was not saved to Dropbox"
                sys.stderr.write(e.error_msg)


class Mailer(object):
    def __init__(self, host, from_, to):
        assert bool(to)

        self._client = smtplib.SMTP(host)
        self._from = from_
        self._to = to

    def send(self, subject, path):
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = self._from
        msg["To"] = ", ".join(self._to)
        msg.preamble = subject

        with open(path, "rb") as f:
            msg.attach(MIMEImage(f.read()))

        r = self._client.sendmail(self._from, self._to, msg.as_string())
        if len(r) == 0:
            print "> SUCCESS: Email was sent to %s" % msg["To"]
        else:
            print "> ERROR: Email was not sent to %s" % ", ".join(r.keys())
            sys.stderr.write(repr(r))

    def __del__(self):
        self._client.quit()


class HomeGuard(object):
    _loaded = False

    _telegram = _DEFAULT_ARG
    _dropbox = _DEFAULT_ARG
    _mailer = _DEFAULT_ARG

    @property
    def telegram(self):
        if HomeGuard._telegram is _DEFAULT_ARG:
            if not config.getboolean("telegram", "enabled"):
                client = DevNull()
            else:
                token = config.get("telegram", "token")
                chats = config.getlist("telegram", "chats")
                client = Telegram(token, chats)
            HomeGuard._telegram = client
        return HomeGuard._telegram

    @property
    def dropbox(self):
        if HomeGuard._dropbox is _DEFAULT_ARG:
            if not config.getboolean("dropbox", "enabled"):
                client = DevNull()
            else:
                token = config.get("dropbox", "token")
                client = Dropbox(token)
            HomeGuard._dropbox = client
        return HomeGuard._dropbox

    @property
    def mailer(self):
        if HomeGuard._mailer is _DEFAULT_ARG:
            if not config.getboolean("mailer", "enabled"):
                client = DevNull()
            else:
                host = config.get("mailer", "host")
                from_ = config.get("mailer", "from")
                to = config.getlist("mailer", "to")
                client = Mailer(host, from_, to)
            HomeGuard._mailer = client
        return HomeGuard._mailer

    def __init__(self):
        if HomeGuard._loaded:
            return

        HomeGuard._loaded = True

        self._channel = config.getint(ConfigParser.DEFAULT_SECTION, "channel")
        self._archive_dir = config.get(ConfigParser.DEFAULT_SECTION, "archive_dir")

        print "HomeGuard %s: Initialisation" % __version__
        if not os.access(self._archive_dir, os.W_OK):
            os.mkdir(self._archive_dir)

        urllib3.disable_warnings()
        GPIO.setmode(GPIO.BCM)

        if config.getboolean("beacon", "enabled"):
            try:
                dt, dx = config.get("beacon", "delta")
                dt = int(dt)

                assert dt > 0
                assert dx in ("m", "h", "d")
            except NoOptionError:
                dt, dx = 1, "d"

            kwargs = {}
            if dx == "m":
                kwargs["minutes"] = dt
            elif dx == "h":
                kwargs["hours"] = dt
            elif dx == "d":
                kwargs["days"] = dt
                hour, minute = config.get("beacon", "time").split(":")
                kwargs["hour"] = int(hour)
                kwargs["minute"] = int(minute)
            self._beacon(**kwargs)

        print "Configuring and starting PIR sensor"
        GPIO.setup(self._channel, GPIO.IN, GPIO.PUD_DOWN)
        while GPIO.input(self._channel):
            pass  # waiting ready

        print "Guard is enabled, detecting motion"
        self.telegram.send_message("Система успешно активирована. Счастливого пути!")

        while True:
            GPIO.wait_for_edge(self._channel, GPIO.RISING)
            self._alarm()

    def __del__(self):
        print "Clean up and exit"
        GPIO.cleanup()

    def _beacon(self, **kwargs):
        def callback():
            self.telegram.send_message("Привет, {0}! Как твои дела? У меня всё хорошо :)")
            self._beacon(**kwargs)

        now = datetime.today()
        if "minutes" in kwargs:
            then = now + timedelta(minutes=kwargs["minutes"])
        elif "hours" in kwargs:
            then = now + timedelta(hours=kwargs["hours"])
        else:  # if "days" in kwargs:
            then = (now + timedelta(days=kwargs["days"])).replace(
                hour=kwargs["hour"], minute=kwargs["minute"], second=0)
        threading.Timer((then - now).seconds, callback).start()

    def _alarm(self):
        timestamp = time.strftime("%d.%m.%Y %H:%M:%S")
        print "ALARM: Motion detected at %s" % timestamp

        message = "Сработал датчик движения в %s" % timestamp
        async(self.telegram.send_message, message)

        if config.getboolean("autoshot", "enabled"):
            series = max(1, config.getint("autoshot", "series"))
            delay = max(1, config.getfloat("autoshot", "delay"))

            with PiCamera() as camera:
                camera.led = False

                archive = os.path.join(self._archive_dir, time.strftime("%Y-%m-%d/%H.%M.%S"))
                output = os.path.join(archive, "img{counter:02d}.jpg")
                if not os.path.exists(archive):
                    os.makedirs(archive)

                for i, path in enumerate(camera.capture_continuous(output)):
                    print '> Photo was saved to "%s"' % path
                    if i == 0:
                        async(self.telegram.send_photo, path)
                        async(self.mailer.send, message, path)

                    async(self.dropbox.put_file, path)

                    if i == series - 1:
                        break

                    time.sleep(delay)

        print "Reporting is done, resume motion detection"

if __name__ == "__main__":
    HomeGuard()
