# SPDX-License-Identifier: AGPL-3.0-or-later

import logging

from kopano.errors import NotFoundError

from .event import EventResource
from .folder import FolderResource
from .resource import _dumpb_json, _start_end, _tzdate, parse_datetime_timezone
from .schema import get_schedule_schema
from .utils import HTTPBadRequest, _folder, _server_store, experimental


def get_fbinfo(req, block):
    return {
        'status': block.status,
        'start': _tzdate(block.start, None, req),
        'end': _tzdate(block.end, None, req)
    }


class CalendarResource(FolderResource):
    fields = FolderResource.fields.copy()
    fields.update({
        'name': lambda folder: folder.name,
    })

    def on_get_calendar_view_by_folderid(self, req, resp, folderid):
        start, end = _start_end(req)
        store = req.context.server_store[1]
        folder = _folder(store, folderid)

        def yielder(**kwargs):
            for occ in folder.occurrences(start, end, **kwargs):
                yield occ
        data = self.generator(req, yielder)
        fields = EventResource.fields
        self.respond(req, resp, data, fields)

    def on_get_calendars(self, req, resp):
        """Handle GET request on 'calendars' endpoint.

        :param req: Falcon request object.
        :type req: Request
        :param resp: Falcon response object.
        :type resp: Response
        """
        store = req.context.server_store[1]
        data = self.generator(req, store.calendars, 0)
        self.respond(req, resp, data, CalendarResource.fields)

    def on_get_calendar(self, req, resp):
        """Handle GET request on 'calendar' endpoint.

        :param req: Falcon request object.
        :type req: Request
        :param resp: Falcon response object.
        :type resp: Response
        """
        store = req.context.server_store[1]
        self.respond(req, resp, store.calendar, self.fields)

    def on_get_calendar_view(self, req, resp):
        start, end = _start_end(req)
        store = req.context.server_store[1]

        def yielder(**kwargs):
            for occ in store.calendar.occurrences(start, end, **kwargs):
                yield occ
        data = self.generator(req, yielder)
        self.respond(req, resp, data, EventResource.fields)

    def on_get_calendar_by_folderid(self, req, resp, folderid):
        """Get a calendar folder by folder ID.

        Args:
            req (Request): Falcon request object.
            req (Response): Falcon response object.
            folderid (str): folder ID.
        """
        store = req.context.server_store[1]
        folder = _folder(store, folderid)
        self.respond(req, resp, folder, self.fields)

    # POST

    @experimental
    def handle_post_schedule(self, req, resp, folder):
        fields = req.context.json_data
        self.validate_json(get_schedule_schema, fields)

        freebusytimes = []

        server, store, userid = _server_store(req, None, self.options)

        email_addresses = fields['schedules']
        start = parse_datetime_timezone(fields['startTime'], 'startTime')
        end = parse_datetime_timezone(fields['endTime'], 'endTime')
        # TODO: implement availabilityView https://docs.microsoft.com/en-us/graph/outlook-get-free-busy-schedule
        availability_view_interval = fields.get('availabilityViewInterval', 60)

        for address in email_addresses:
            try:
                user = server.user(email=address)
            except NotFoundError:
                continue  # TODO: silent ignore or raise exception?

            fbdata = {
                'scheduleId': address,
                'availabilityView': '',
                'scheduleItems': [],
                'workingHours': {},
            }

            try:
                blocks = user.freebusy.blocks(start=start, end=end)
            except NotFoundError:
                logging.warning('no public store available, unable to retrieve freebusy data')
                continue

            if not blocks:
                continue

            fbdata['scheduleItems'] = [get_fbinfo(req, block) for block in blocks]
            freebusytimes.append(fbdata)

        data = {
            "@odata.context": "https://graph.microsoft.com/v1.0/$metadata#Collection(microsoft.graph.scheduleInformation)",
            "value": freebusytimes,
        }
        resp.content_type = 'application/json'
        resp.body = _dumpb_json(data)

    def on_post(self, req, resp, userid=None, folderid=None, method=None):
        handler = None

        if method == 'getSchedule':
            handler = self.handle_post_schedule

        elif method:
            raise HTTPBadRequest("Unsupported calendar segment '%s'" % method)

        else:
            raise HTTPBadRequest("Unsupported in calendar")

        server, store, userid = req.context.server_store
        folder = _folder(store, folderid or 'calendar')
        handler(req, resp, folder=folder)
