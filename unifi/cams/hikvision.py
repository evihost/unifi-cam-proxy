import argparse
import asyncio
import logging
import tempfile
import threading
import traceback
from pathlib import Path
from typing import Any, Dict

import backoff
import requests
import websockets
import xmltodict
from hikvisionapi import Client

from unifi.cams.base import UnifiCamBase
from unifi.core import RetryableError


class HikvisionCam(UnifiCamBase):
    def __init__(self, args: argparse.Namespace, logger: logging.Logger) -> None:
        super().__init__(args, logger)
        self.snapshot_dir = tempfile.mkdtemp()
        self.streams = {}
        self.motion_timeout = 5  # seconds
        self.motion_counter = 0
        self.loop = None
        self.cam = Client(
            f"http://{self.args.ip}:{self.args.httpport}", self.args.username, self.args.password
        )
        self.ptz_supported = self.check_ptz_support()

    @classmethod
    def add_parser(cls, parser: argparse.ArgumentParser) -> None:
        super().add_parser(parser)
        parser.add_argument("--username", "-u", required=True, help="Camera username")
        parser.add_argument("--password", "-p", required=True, help="Camera password")
        parser.add_argument("--httpport", "-hp", default="80", help="HTTP Port")
        parser.add_argument("--rtspport", "-rp", default="554", help="RTSP Port")

    async def get_snapshot(self) -> Path:
        img_file = Path(self.snapshot_dir, "screen.jpg")

        resp = self.cam.Streaming.channels[102].picture(
            method="get", type="opaque_data"
        )
        with img_file.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
        return img_file

    def check_ptz_support(self) -> bool:
        try:
            self.cam.PTZCtrl.channels[1].capabilities(method="get")
            self.logger.info("Detected PTZ support")
            return True
        except requests.exceptions.HTTPError:
            pass
        return False

    def get_video_settings(self) -> Dict[str, Any]:
        if self.ptz_supported:
            r = self.cam.PTZCtrl.channels[1].status(method="get")["PTZStatus"][
                "AbsoluteHigh"
            ]
            return {
                # Tilt/elevation
                "brightness": int(100 * int(r["azimuth"]) / 3600),
                # Pan/azimuth
                "contrast": int(100 * int(r["azimuth"]) / 3600),
                # Zoom
                "hue": int(100 * int(r["absoluteZoom"]) / 40),
            }
        return {}

    def change_video_settings(self, options: Dict[str, Any]) -> None:
        if self.ptz_supported:
            tilt = int((900 * int(options["brightness"])) / 100)
            pan = int((3600 * int(options["contrast"])) / 100)
            zoom = int((40 * int(options["hue"])) / 100)

            self.logger.info("Moving to %s:%s:%s", pan, tilt, zoom)
            req = {
                "PTZData": {
                    "@version": "2.0",
                    "@xmlns": "http://www.hikvision.com/ver20/XMLSchema",
                    "AbsoluteHigh": {
                        "absoluteZoom": str(zoom),
                        "azimuth": str(pan),
                        "elevation": str(tilt),
                    },
                }
            }
            self.cam.PTZCtrl.channels[1].absolute(
                method="put", data=xmltodict.unparse(req, pretty=True)
            )

    def get_stream_source(self, stream_index: str) -> str:
        channel = 1
        if stream_index != "video1":
            channel = 3
        return (
            f"rtsp://{self.args.username}:{self.args.password}@{self.args.ip}:{self.args.rtspport}"
        )

    async def _trigger_motion_start(self):
        if self.motion_counter == 0:
            await self.trigger_motion_start()
            print('Motion detected - recording started')
        self.motion_counter += 1
        self.loop.call_later(self.motion_timeout, asyncio.ensure_future, self._trigger_motion_stop())

    async def _trigger_motion_stop(self):
        self.motion_counter -= 1
        if self.motion_counter <= 0:
            self.motion_counter = 0
            print('Motion ended - recording stopped')
            await self.trigger_motion_stop()

    def _worker(self):
        while True:
            try:
                self.cam.count_events = 1  # The number of events we want to retrieve (default = 1)
                response = self.cam.Event.notification.alertStream(method='get', type='stream')
                if response:
                    for r in response:
                        event = r['EventNotificationAlert']
                        if event['eventType'] == 'VMD' and event['eventDescription'] == 'Motion alarm':
                            asyncio.run_coroutine_threadsafe(self._trigger_motion_start(), self.loop)
            except:
                traceback.print_exc()

    async def run(self) -> None:
        self.loop = asyncio.get_event_loop()
        threading.Thread(target=self._worker, daemon=True).start()

