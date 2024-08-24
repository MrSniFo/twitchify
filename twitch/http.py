"""
The MIT License (MIT)

Copyright (c) 2024-present Snifo

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

from .errors import (HTTPException, TwitchServerError, Forbidden, NotFound, AuthFailure)
from urllib.parse import quote as _uriquote
from . import __version__, __github__
from .utils import json_or_text
import aiohttp
import asyncio
import socket
import time

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .types import (Data, DTData, Edata, PData, TData, TPData, activity, analytics, bits, chat, channels,
                        interaction, moderation, search, streams, users, PEdata, TTMData)
    from typing import Any, ClassVar, Coroutine, Dict, List, Literal, Optional, TypeVar, Union

    T = TypeVar('T')
    Response = Coroutine[Any, Any, T]

import logging
_logger = logging.getLogger(__name__)

__all__ = ('HTTPClient',)


class Route:
    """Represents HTTP route."""
    BASE: ClassVar[str] = 'https://api.twitch.tv/helix/'
    OAUTH2: ClassVar[str] = 'https://id.twitch.tv/oauth2/'

    __slots__ = ('method', 'url')

    def __init__(self, method: str, path: str, oauth2: bool = False, **parameters: Any) -> None:
        self.method: str = method
        base_url = self.OAUTH2 if oauth2 else self.BASE

        if parameters:
            # Filter out None values from parameters
            filtered_params = {k: v for k, v in parameters.items() if v is not None}
            if filtered_params:
                query_string = '?' + '&'.join(
                    f'{k}={_uriquote(str(i))}' for k, v in filtered_params.items() for i in
                    (v if isinstance(v, list) else [v])
                )
            else:
                query_string = ''
        else:
            query_string = ''

        self.url: str = f'{base_url}{path}{query_string}'

    def __repr__(self) -> str:
        return f'<Route method={self.method} url={self.url}>'


class HTTPClient:
    """Represents an asynchronous HTTP client for sending HTTP requests."""

    def __init__(self,
                 client_id: str,
                 client_secret: Optional[str],
                 *,
                 loop: Optional[asyncio.AbstractEventLoop] = None,
                 proxy: Optional[str] = None,
                 proxy_auth: Optional[aiohttp.BasicAuth] = None,
                 cli: bool = False,
                 cli_port: int = 8080) -> None:
        # Debugging mode.
        self.cli: bool = cli
        self.cli_port: int = cli_port
        # HTTP
        self.proxy: Optional[str] = proxy
        self.proxy_auth: Optional[aiohttp.BasicAuth] = proxy_auth
        self.__session: Optional[aiohttp.ClientSession] = None
        self.user_agent: str = f'Twitchify/{__version__} (GitHub: {__github__})'
        # Twitch API authentication credentials.
        self.client_id: str = client_id
        self.client_secret: Optional[str] = client_secret
        self.access_token: Optional[str] = None
        # Task for keeping the session alive.
        self.loop: asyncio.AbstractEventLoop = loop
        self.__keep_alive: Optional[asyncio.Task] = None

    async def close(self) -> None:
        """
        Closes the underlying session if it exists.
        """
        if self.__session:
            await self.__session.close()

    async def clear(self) -> None:
        """
        Clears the session and cancels any pending keep-alive tasks.
        """
        if self.__session and self.__session.closed:
            self.__session = None
        if self.__keep_alive is not None and not self.__keep_alive.done():
            self.__keep_alive.cancel()
        self.__keep_alive = None

    async def ws_connect(self, url: str,
                         *,
                         compress: int = 0,
                         reconnect: bool = False) -> aiohttp.ClientWebSocketResponse:
        """
        Establishes a WebSocket connection to the specified URL with optional compression.
        """
        kwargs = {
            'max_msg_size': 0,
            'timeout': 30.0,
            'proxy_auth': self.proxy_auth,
            'proxy': self.proxy,
            'autoclose': False,
            'headers': {
                'User-Agent': self.user_agent,
            },
            'compress': compress,
        }
        url: str = f'ws://localhost:{self.cli_port}/ws' if (self.cli and not reconnect) else url
        return await self.__session.ws_connect(url, **kwargs)

    async def request(self, route: Route, **kwargs: Any) -> Any:
        """Make an HTTP request with the provided route and keyword arguments."""
        method = route.method
        url = route.url

        if 'headers' not in kwargs:
            headers: Dict[str, str] = {'Client-ID': self.client_id}
            if self.access_token is not None:
                headers['Authorization'] = 'Bearer ' + self.access_token
            if 'json' in kwargs:
                headers['Content-Type'] = 'application/json'
                kwargs['json'] = kwargs.pop('json')
            kwargs['headers'] = headers

        # Headers.
        kwargs['headers']['User-Agent'] = self.user_agent

        # Proxy support.
        if self.proxy is not None:
            kwargs['proxy'] = self.proxy
        if self.proxy_auth is not None:
            kwargs['proxy_auth'] = self.proxy_auth

        for attempt in range(3):
            try:
                async with self.__session.request(method, url, **kwargs) as response:
                    _logger.debug('%s >> %s with %s has returned status code %s', method, url,
                                  kwargs.get('data'), response.status)

                    data = await json_or_text(response)
                    if 300 > response.status >= 200:
                        _logger.debug('%s << %s has received %s', method, url, data)
                        return data
                    if response.status == 403:
                        raise Forbidden(response, data)
                    elif response.status == 404:
                        raise NotFound(response, data)
                    elif response.status >= 500:
                        raise TwitchServerError(response, data)
                    else:
                        raise HTTPException(response, data)
            except OSError as e:
                if attempt < 2 and e.errno in (54, 10054):
                    await asyncio.sleep(1 + attempt * 2)
                    continue
                raise

    async def authorize(self, access_token: Optional[str], refresh_token: Optional[str]) -> users.OAuthToken:
        """Authorize the client with the provided access token and optional refresh token."""
        await self.clear()
        connector = aiohttp.TCPConnector(limit=0, family=socket.AF_INET)
        self.__session = aiohttp.ClientSession(connector=connector)
        for _ in range(2):
            try:
                if access_token is None and refresh_token:
                    raise AuthFailure
                data = await self.validate_token(access_token)
                self.access_token = access_token
                # Keeps the user's access token fresh.
                self.__keep_alive = self.loop.create_task(self.keep_alive(refresh_token, interval=data['expires_in']),
                                                          name='Twitchify:keep_alive')
            except (HTTPException, AuthFailure) as exc:
                if refresh_token and isinstance(exc, AuthFailure):
                    data = await self.refresh_token(refresh_token)
                    access_token = data['access_token']
                    _logger.debug('A new access token has been generated.')
                    continue
                raise
            return data

    async def validate_token(self, access_token: str) -> users.OAuthToken:
        """Validate the user's access token."""
        try:
            headers: Dict[str, str] = {'Client-ID': self.client_id, 'Authorization': 'Bearer ' + access_token}
            data = await self.request(Route('GET', 'validate', oauth2=True), headers=headers)
            return data
        except HTTPException as exc:
            if exc.status == 401:
                raise AuthFailure('Improper access token has been passed.') from exc
            raise

    async def refresh_token(self, refresh_token: str) -> users.OAuthRefreshToken:
        """Regenerate the user's access token using the provided refresh token."""
        body: Dict[str, str] = {'grant_type': 'refresh_token',
                                'refresh_token': refresh_token,
                                'client_secret': self.client_secret,
                                'client_id': self.client_id}
        return await self.request(Route('POST', 'token', oauth2=True), data=body)

    async def keep_alive(self, refresh_token: Optional[str] = None, *, interval: int) -> None:
        """Keeps the Access Token alive, validating and generating it."""
        start_time = time.time()
        if refresh_token is not None:
            _logger.debug('A new token will be generated in %s seconds.', interval - 300)
        else:
            # Validating every 1 hour.
            interval = 3540 + 300
            _logger.warning('Access token will expire in %s seconds, and won\'t be generated without '
                            'the refresh token or client secret.', interval - 300)
        while True:
            # Create a new access token approximately 5 minutes before the current token's expiration.
            await asyncio.sleep(min((interval - 300), 3540))
            current_time = time.time()
            elapsed_time = current_time - start_time
            try:
                if refresh_token is not None and (elapsed_time >= interval - 300):
                    # Reset the refresh token timer.
                    start_time = time.time()
                    generate = await self.refresh_token(refresh_token)
                    self.access_token = generate['access_token']
                    refresh_token = generate['refresh_token']
                validation = await self.validate_token(self.access_token)
                # Update the expiration time of the access token.
                interval = validation['expires_in']
            except HTTPException as exc:
                _logger.warning('%s, Auto access token generation feature has been disabled.', exc.text)
                refresh_token = None
                continue

    @staticmethod
    def get_subscription_info(event: str) -> Optional[Dict[str, Any]]:
        # Warning: This dictionary may be updated anytime based on new event types or API changes.
        # It maps subscription types to their respective Twitch event name and version.
        subscriptions: Dict[str, Dict[str, Any]] = {
            'automod_message_hold': {
                'name': 'automod.message.hold',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'automod_message_update': {
                'name': 'automod.message.update',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'automod_settings_update': {
                'name': 'automod.settings.update',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'automod_terms_update': {
                'name': 'automod.terms.update',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'channel_update': {
                'name': 'channel.update',
                'version': '2',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'follow': {
                'name': 'channel.follow',
                'version': '2',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'ad_break_begin': {
                'name': 'channel.ad_break.begin',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'chat_clear': {
                'name': 'channel.chat.clear',
                'version': '1',
                'condition': {'client': 'user_id', 'user': 'broadcaster_user_id'}
            },
            'chat_clear_user_messages': {
                'name': 'channel.chat.clear_user_messages',
                'version': '1',
                'condition': {'client': 'user_id', 'user': 'broadcaster_user_id'}
            },
            'chat_message': {
                'name': 'channel.chat.message',
                'version': '1',
                'condition': {'client': 'user_id', 'user': 'broadcaster_user_id'}
            },
            'chat_message_delete': {
                'name': 'channel.chat.message_delete',
                'version': '1',
                'condition': {'client': 'user_id', 'user': 'broadcaster_user_id'}
            },
            'chat_notification': {
                'name': 'channel.chat.notification',
                'version': '1',
                'condition': {'client': 'user_id', 'user': 'broadcaster_user_id'}
            },
            'chat_settings_update': {
                'name': 'channel.chat_settings.update',
                'version': '1',
                'condition': {'client': 'user_id', 'user': 'broadcaster_user_id'}
            },
            'chat_user_message_hold': {
                'name': 'channel.chat.user_message_hold',
                'version': '1',
                'condition': {'client': 'user_id', 'user': 'broadcaster_user_id'}
            },
            'chat_user_message_update': {
                'name': 'channel.chat.user_message_update',
                'version': '1',
                'condition': {'client': 'user_id', 'user': 'broadcaster_user_id'}
            },
            'subscribe': {
                'name': 'channel.subscribe',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'subscription_end': {
                'name': 'channel.subscription.end',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'subscription_gift': {
                'name': 'channel.subscription.gift',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'subscription_message': {
                'name': 'channel.subscription.message',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'cheer': {
                'name': 'channel.cheer',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'raid': {
                'name': 'channel.raid',
                'version': '1',
                'condition': {'client': None, 'user': 'to_broadcaster_user_id'}
            },
            'ban': {
                'name': 'channel.ban',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'unban': {
                'name': 'channel.unban',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'unban_request_create': {
                'name': 'channel.unban_request.create',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'unban_request_resolve': {
                'name': 'channel.unban_request.resolve',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'moderate': {
                'name': 'channel.moderate',
                'version': '2',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'moderator_add': {
                'name': 'channel.moderator.add',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'moderator_remove': {
                'name': 'channel.moderator.remove',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'points_automatic_reward_redemption_add': {
                'name': 'channel.channel_points_automatic_reward_redemption.add',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'points_reward_add': {
                'name': 'channel.channel_points_custom_reward.add',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'points_reward_update': {
                'name': 'channel.channel_points_custom_reward.update',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'points_reward_remove': {
                'name': 'channel.channel_points_custom_reward.remove',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'points_reward_redemption_add': {
                'name': 'channel.channel_points_custom_reward_redemption.add',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'points_reward_redemption_update': {
                'name': 'channel.channel_points_custom_reward_redemption.update',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'poll_begin': {
                'name': 'channel.poll.begin',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'poll_progress': {
                'name': 'channel.poll.progress',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'poll_end': {
                'name': 'channel.poll.end',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'prediction_begin': {
                'name': 'channel.prediction.begin',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'prediction_progress': {
                'name': 'channel.prediction.progress',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'prediction_lock': {
                'name': 'channel.prediction.lock',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'prediction_end': {
                'name': 'channel.prediction.end',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'suspicious_user_message': {
                'name': 'channel.suspicious_user.message',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'suspicious_user_update': {
                'name': 'channel.suspicious_user.update',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'vip_add': {
                'name': 'channel.vip.add',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'vip_remove': {
                'name': 'channel.vip.remove',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'warning_acknowledge': {
                'name': 'channel.warning.acknowledge',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'warning_send': {
                'name': 'channel.warning.send',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'charity_campaign_donate': {
                'name': 'channel.charity_campaign.donate',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'charity_campaign_start': {
                'name': 'channel.charity_campaign.start',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'charity_campaign_progress': {
                'name': 'channel.charity_campaign.progress',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'charity_campaign_stop': {
                'name': 'channel.charity_campaign.stop',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'goal_begin': {
                'name': 'channel.goal.begin',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'goal_progress': {
                'name': 'channel.goal.progress',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'goal_end': {
                'name': 'channel.goal.end',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'hype_train_begin': {
                'name': 'channel.hype_train.begin',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'hype_train_progress': {
                'name': 'channel.hype_train.progress',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'hype_train_end': {
                'name': 'channel.hype_train.end',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'shield_mode_begin': {
                'name': 'channel.shield_mode.begin',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'shield_mode_end': {
                'name': 'channel.shield_mode.end',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'shoutout_create': {
                'name': 'channel.shoutout.create',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'shoutout_received': {
                'name': 'channel.shoutout.receive',
                'version': '1',
                'condition': {'client': 'moderator_user_id', 'user': 'broadcaster_user_id'}
            },
            'stream_online': {
                'name': 'stream.online',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'stream_offline': {
                'name': 'stream.offline',
                'version': '1',
                'condition': {'client': None, 'user': 'broadcaster_user_id'}
            },
            'user_authorization_grant': {
                'name': 'user.authorization.grant',
                'version': '1',
                'condition': {'client': 'client_id', 'user': None}
            },
            'user_authorization_revoke': {
                'name': 'user.authorization.revoke',
                'version': '1',
                'condition': {'client': 'client_id', 'user': None}
            },
            'user_update': {
                'name': 'user.update',
                'version': '1',
                'condition': {'client': None, 'user': 'user_id'}
            },
            'whisper_received': {
                'name': 'user.whisper.message',
                'version': '1',
                'condition': {'client': None, 'user': 'user_id'}
            }
        }
        return subscriptions.get(event)

    def create_subscription(
            self,
            client_user_id: str,
            user_id: str,
            session_id: str,
            *,
            subscription_type: str,
            subscription_version: str,
            subscription_condition: Dict[str, Any],
            subscription_condition_options: Optional[Dict[str, Any]] = None
    ) -> Response[TTMData[List[users.EventSubSubscription]]]:
        """
        Create an EventSub subscription.
        """
        route = Route('POST', 'eventsub/subscriptions')
        if self.cli:
            route.url = f'http://localhost:{self.cli_port}/eventsub/subscriptions'

        condition = {}

        # Ensure 'client' key is properly assigned
        client_key = subscription_condition.get('client')
        if client_key:
            condition[client_key] = self.client_id if client_key == 'client_id' else client_user_id

        # Ensure 'user' key is properly assigned
        user_key = subscription_condition.get('user')
        if user_key:
            condition[user_key] = user_id

        if subscription_condition_options:
            condition.update(subscription_condition_options)

        body = {
            'type': subscription_type,
            'version': subscription_version,
            'condition': condition,
            'transport': {
                'method': 'websocket',
                'session_id': session_id
            }
        }
        return self.request(route=route, json=body)

    def delete_subscription(self, subscription_id: str) -> Response[None]:
        params: Dict[str, Any] = {
            'id': subscription_id
        }
        return self.request(Route('DELETE', 'eventsub/subscriptions', **params))

    # Ads
    def start_commercial(self,
                         broadcaster_id: str,
                         length: int = 180
                         ) -> Response[Data[List[streams.CommercialStatus]]]:
        body = {
            'broadcaster_id': broadcaster_id,
            'length': length
        }
        return self.request(Route('POST', 'channels/commercial'), data=body)

    def get_ad_schedule(self, broadcaster_id: str) -> Response[Data[List[streams.AdSchedule]]]:
        return self.request(Route('GET', 'channels/ads', broadcaster_id=broadcaster_id))

    def snooze_next_ad(self, broadcaster_id: str) -> Response[Data[List[streams.AdSnooze]]]:
        return self.request(Route('POST', 'channels/ads/schedule/snooze', broadcaster_id=broadcaster_id))

    # Analytics
    def get_extension_analytics(self,
                                extension_id: Optional[str] = None,
                                analytics_type: Literal['overview_v2'] = 'overview_v2',
                                started_at: Optional[str] = None,
                                ended_at: Optional[str] = None,
                                first: int = 20,
                                after: Optional[str] = None) -> Response[PData[List[analytics.Extension]]]:
        params: Dict[str, Any] = {
            'extension_id': extension_id,
            'type': analytics_type,
            'started_at': started_at,
            'ended_at': ended_at,
            'first': first,
            'after': after
        }
        return self.request(Route('GET', 'analytics/extensions', **params))

    def get_game_analytics(self,
                           game_id: Optional[str] = None,
                           analytics_type: Literal['overview_v2'] = 'overview_v2',
                           started_at: Optional[str] = None,
                           ended_at: Optional[str] = None,
                           first: int = 20,
                           after: Optional[str] = None) -> Response[PData[List[analytics.Game]]]:
        params: Dict[str, Any] = {
            'game_id': game_id,
            'type': analytics_type,
            'started_at': started_at,
            'ended_at': ended_at,
            'first': first,
            'after': after
        }
        return self.request(Route('GET', 'analytics/games', **params))

    # Bits
    def get_bits_leaderboard(self,
                             period: Optional[Literal['day', 'week', 'month', 'year', 'all']],
                             started_at: Optional[str],
                             user_id: Optional[str],
                             count: int = 10) -> Response[DTData[List[bits.Leaderboard]]]:
        params: Dict[str, Any] = {'count': count,
                                  'period': period,
                                  'started_at': started_at,
                                  'user_id': user_id
                                  }
        return self.request(Route('GET', 'bits/leaderboard', **params))

    def get_cheermotes(self, broadcaster_id: Optional[str] = None) -> Response[Data[List[bits.Cheermote]]]:
        return self.request(Route('GET', 'bits/cheermotes', broadcaster_id=broadcaster_id))

    # Channel
    def get_channel_information(self, broadcaster_ids: List[str]) -> Response[Data[List[channels.ChannelInfo]]]:
        return self.request(Route('GET', 'channels', broadcaster_id=broadcaster_ids))

    def modify_channel_information(self,
                                   broadcaster_id: str,
                                   category_id: Optional[str] = None,
                                   broadcaster_language: Optional[str] = None,
                                   title: Optional[str] = None,
                                   delay: Optional[int] = None,
                                   tags: Optional[List[str]] = None,
                                   content_classification_labels: Optional[List[channels.CCL]] = None,
                                   is_branded_content: Optional[bool] = None) -> Response[None]:
        body = {
            'game_id': category_id,
            'broadcaster_language': broadcaster_language,
            'title': title,
            'delay': delay,
            'tags': tags,
            'content_classification_labels': content_classification_labels,
            is_branded_content: is_branded_content,
        }
        body = {key: value for key, value in body.items() if value is not None}
        return self.request(Route('PATCH', 'channels', broadcaster_id=broadcaster_id), data=body)

    def get_channel_editors(self, broadcaster_id: str) -> Response[Data[List[channels.Editor]]]:
        return self.request(Route('GET', 'channels/editors', broadcaster_id=broadcaster_id))

    def get_followed_channels(self,
                              user_id: str,
                              *,
                              broadcaster_id: Optional[str] = None,
                              after: Optional[str] = None,
                              first: int = 20) -> Response[TData[List[channels.Follows]]]:
        params: Dict[str, Any] = {'user_id': user_id,
                                  'broadcaster_id': broadcaster_id,
                                  'after': after,
                                  'first': first}

        return self.request(Route('GET', 'channels/followed', **params))

    def get_channel_followers(self,
                              broadcaster_id: str,
                              *,
                              user_id: Optional[str] = None,
                              after: Optional[str] = None,
                              first: int = 20) -> Response[TData[List[channels.Follower]]]:
        params: Dict[str, Any] = {'broadcaster_id': broadcaster_id,
                                  'user_id': user_id,
                                  'after': after,
                                  'first': first}

        return self.request(Route('GET', 'channels/followers', **params))

    # Channel Points
    def create_custom_rewards(self,
                              broadcaster_id: str,
                              title: str,
                              cost: int,
                              is_enabled: bool = True,
                              background_color: Optional[str] = None,
                              is_user_input_required: bool = False,
                              prompt: Optional[str] = None,
                              is_max_per_stream_enabled: bool = False,
                              max_per_stream: Optional[int] = None,
                              is_max_per_user_per_stream_enabled: bool = False,
                              max_per_user_per_stream: Optional[int] = None,
                              is_global_cooldown_enabled: bool = False,
                              global_cooldown_seconds: Optional[int] = None,
                              should_redemptions_skip_request_queue: bool = False
                              ) -> Response[Data[List[interaction.Reward]]]:
        body: Dict[str, Any] = {
            'title': title,
            'cost': cost,
            'prompt': prompt,
            'is_enabled': is_enabled,
            'background_color': background_color,
            'is_user_input_required': is_user_input_required,
            'is_max_per_stream_enabled': is_max_per_stream_enabled,
            'max_per_stream': max_per_stream,
            'is_max_per_user_per_stream_enabled': is_max_per_user_per_stream_enabled,
            'max_per_user_per_stream': max_per_user_per_stream,
            'is_global_cooldown_enabled': is_global_cooldown_enabled,
            'global_cooldown_seconds': global_cooldown_seconds,
            'should_redemptions_skip_request_queue': should_redemptions_skip_request_queue
        }
        body = {key: value for key, value in body.items() if value is not None}
        return self.request(Route('POST', 'channel_points/custom_rewards', broadcaster_id=broadcaster_id),
                            data=body)

    def delete_custom_reward(self, broadcaster_id: str, reward_id: str) -> Response[None]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'id': reward_id
        }
        return self.request(Route('DELETE', 'channel_points/custom_rewards', **params))

    def get_custom_reward(self,
                          broadcaster_id: str,
                          reward_ids: Optional[List[str]],
                          only_manageable_rewards: bool = False
                          ) -> Response[Data[List[interaction.Reward]]]:

        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'id': reward_ids,
            'only_manageable_rewards': only_manageable_rewards
        }

        return self.request(Route('GET', 'channel_points/custom_rewards', **params))

    def get_custom_reward_redemption(self,
                                     broadcaster_id: str,
                                     reward_id: str,
                                     redemption_ids: Optional[List[str]],
                                     status: Optional[Literal['canceled', 'fulfilled', 'unfulfilled']] = None,
                                     after: Optional[str] = None,
                                     sort: Literal['oldest', 'newest'] = 'oldest',
                                     first: int = 20
                                     ) -> Response[PData[List[interaction.RewardRedemption]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'reward_id': reward_id,
            'status': status.upper() if status is not None else None,
            'redemption_ids': redemption_ids,
            'after': after,
            'sort': sort.upper(),
            'first': first,
        }

        return self.request(Route('GET', 'channel_points/custom_rewards/redemptions', **params))

    def update_custom_reward(self,
                             broadcaster_id: str,
                             reward_id: str,
                             title: Optional[str] = None,
                             cost: Optional[int] = None,
                             is_enabled: Optional[bool] = None,
                             background_color: Optional[str] = None,
                             is_user_input_required: Optional[bool] = None,
                             prompt: Optional[str] = None,
                             is_max_per_stream_enabled: Optional[bool] = None,
                             max_per_stream: Optional[int] = None,
                             is_max_per_user_per_stream_enabled: Optional[bool] = None,
                             max_per_user_per_stream: Optional[int] = None,
                             is_global_cooldown_enabled: Optional[bool] = None,
                             global_cooldown_seconds: Optional[int] = None,
                             should_redemptions_skip_request_queue: Optional[bool] = None
                             ) -> Response[Data[List[interaction.Reward]]]:

        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'id': reward_id,
        }

        body: Dict[str, Any] = {
            'title': title,
            'cost': cost,
            'prompt': prompt,
            'is_enabled': is_enabled,
            'background_color': background_color,
            'is_user_input_required': is_user_input_required,
            'is_max_per_stream_enabled': is_max_per_stream_enabled,
            'max_per_stream': max_per_stream,
            'is_max_per_user_per_stream_enabled': is_max_per_user_per_stream_enabled,
            'max_per_user_per_stream': max_per_user_per_stream,
            'is_global_cooldown_enabled': is_global_cooldown_enabled,
            'global_cooldown_seconds': global_cooldown_seconds,
            'should_redemptions_skip_request_queue': should_redemptions_skip_request_queue
        }
        body = {key: value for key, value in body.items() if value is not None}
        return self.request(Route('PATCH', 'channel_points/custom_rewards', **params), data=body)

    def update_redemption_status(self,
                                 broadcaster_id: str,
                                 reward_id: str,
                                 redemption_ids: List[str],
                                 *,
                                 status: Literal['canceled', 'fulfilled']
                                 ) -> Response[Data[List[interaction.RewardRedemption]]]:

        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'id': redemption_ids,
            'reward_id': reward_id
        }

        body: Dict[str, Any] = {
            'status': status.upper(),
        }
        return self.request(Route('PATCH', 'channel_points/custom_rewards/redemptions', **params),
                            data=body)

    # Charity
    def get_charity_campaign(self, broadcaster_id: str) -> Response[Data[List[activity.Charity]]]:
        return self.request(Route('GET', 'charity/campaigns', broadcaster_id=broadcaster_id))

    def get_charity_campaign_donations(self,
                                       broadcaster_id: str,
                                       after: Optional[str],
                                       first: int = 20) -> Response[PData[List[activity.CharityDonation]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'first': first,
            'after': after
        }

        return self.request(Route('GET', 'charity/donations', **params))

    # Chat
    def get_chatters(self,
                     broadcaster_id: str,
                     moderator_id: str,
                     after: Optional[str] = None,
                     first: int = 20) -> Response[TData[List[users.SpecificUser]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id,
            'first': first,
            'after': after
        }
        return self.request(Route('GET', 'chat/chatters', **params))

    def get_channel_emotes(self, broadcaster_id: str) -> Response[Edata[List[chat.Emote]]]:
        return self.request(Route('GET', 'chat/emotes', broadcaster_id=broadcaster_id))

    def get_global_emotes(self) -> Response[Edata[List[chat.Emote]]]:
        return self.request(Route('GET', 'chat/emotes/global'))

    def get_emote_sets(self, emote_set_ids: List[str]) -> Response[Edata[List[chat.Emote]]]:
        return self.request(Route('GET', 'chat/emotes/set', emote_set_id=emote_set_ids))

    def get_user_emotes(self,
                        user_id: str,
                        broadcaster_id: Optional[str] = None,
                        after: Optional[str] = None) -> Response[PEdata[List[chat.Emote]]]:
        params: Dict[str, Any] = {
            'user_id': user_id,
            'broadcaster_id': broadcaster_id,
            'after': after
        }
        return self.request(Route('GET', 'chat/emotes/user', **params))

    def get_channel_chat_badges(self, broadcaster_id: str) -> Response[Data[List[chat.Badge]]]:
        return self.request(Route('GET', 'chat/badges', broadcaster_id=broadcaster_id))

    def get_global_chat_badges(self) -> Response[Data[List[chat.Badge]]]:
        return self.request(Route('GET', 'chat/badges/global'))

    def get_chat_settings(self,
                          broadcaster_id: str,
                          moderator_id: Optional[str]) -> Response[Data[List[chat.Settings]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id
        }

        return self.request(Route('GET', 'chat/settings', **params))

    def update_chat_settings(
            self,
            broadcaster_id: str,
            moderator_id: str,
            *,
            emote_mode: Optional[bool] = None,
            follower_mode: Optional[bool] = None,
            follower_mode_duration: Optional[int] = None,
            non_moderator_chat_delay: Optional[bool] = None,
            non_moderator_chat_delay_duration: Optional[int] = None,
            slow_mode: Optional[bool] = None,
            slow_mode_wait_time: Optional[int] = None,
            subscriber_mode: Optional[bool] = None,
            unique_chat_mode: Optional[bool] = None) -> Response[Data[List[chat.Settings]]]:

        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id
        }

        body: Dict[str, Any] = {
            'emote_mode': emote_mode,
            'follower_mode': follower_mode,
            'follower_mode_duration': follower_mode_duration if follower_mode else None,
            'non_moderator_chat_delay': non_moderator_chat_delay,
            'non_moderator_chat_delay_duration': (non_moderator_chat_delay_duration
                                                  if non_moderator_chat_delay else None),
            'slow_mode': slow_mode,
            'slow_mode_wait_time': slow_mode_wait_time if slow_mode else None,
            'subscriber_mode': subscriber_mode,
            'unique_chat_mode': unique_chat_mode
        }

        body = {key: value for key, value in body.items() if value is not None}
        return self.request(Route('PATCH', 'chat/settings', **params), data=body)

    def send_chat_announcement(self,
                               broadcaster_id: str,
                               moderator_id: str,
                               *,
                               message: str,
                               color: Literal['blue', 'green', 'orange', 'purple', 'primary'] = 'primary'
                               ) -> Response[None]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id
        }

        body: Dict[str, Any] = {
            'message': message,
            'color': color,
        }
        return self.request(Route('POST', 'chat/announcements', **params), data=body)

    def send_a_shoutout(self,
                        from_broadcaster_id: str,
                        moderator_id: str, *,
                        to_broadcaster_id: str,
                        ) -> Response[None]:
        params: Dict[str, Any] = {
            'from_broadcaster_id': from_broadcaster_id,
            'to_broadcaster_id': to_broadcaster_id,
            'moderator_id': moderator_id
        }
        return self.request(Route('POST', 'chat/shoutouts', **params))

    def send_chat_message(self,
                          broadcaster_id: str,
                          sender_id: str,
                          text: str,
                          reply_parent_message_id: Optional[str] = None
                          ) -> Response[Data[List[chat.SendMessageStatus]]]:

        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'sender_id': sender_id,
            'message': text,
            'reply_parent_message_id': reply_parent_message_id
        }

        return self.request(Route('POST', 'chat/messages', **params))

    def get_user_chat_color(self, user_ids: List[str]) -> Response[Data[List[chat.UserChatColor]]]:
        return self.request(Route('GET', 'chat/color', user_id=user_ids))

    def update_user_chat_color(self,
                               user_id: str,
                               color: Union[str, chat.UserChatColors]) -> Response[None]:
        return self.request(Route('PUT', 'chat/color', user_id=user_id, color=color))

    # Clips
    def create_clip(self,
                    broadcaster_id: str,
                    *,
                    has_delay: bool = False
                    ) -> Response[Data[List[channels.ClipEdit]]]:
        return self.request(Route('POST', 'clips', broadcaster_id=broadcaster_id, has_delay=has_delay))

    def get_clips(self,
                  broadcaster_id: Optional[str] = None,
                  game_id: Optional[str] = None,
                  clip_ids: Optional[List[str]] = None,
                  started_at: Optional[str] = None,
                  ended_at: Optional[str] = None,
                  first: int = 20,
                  before: Optional[str] = None,
                  after: Optional[str] = None,
                  is_featured: Optional[bool] = None
                  ) -> Response[PData[List[channels.Clip]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'game_id': game_id,
            'ids': clip_ids,
            'started_at': started_at,
            'ended_at': ended_at,
            'first': first,
            'before': before,
            'after': after,
            'is_featured': is_featured
        }
        return self.request(Route('GET', 'clips', **params))

    # CCLs
    def get_content_classification_labels(self,
                                          locale: streams.Locale = 'en-US'
                                          ) -> Response[Data[List[streams.CCLInfo]]]:
        return self.request(Route('GET', 'content_classification_labels', locale=locale))

    # Entitlements
    def get_drops_entitlements(self,
                               entitlement_ids: Optional[List[str]] = None,
                               user_id: Optional[str] = None,
                               game_id: Optional[str] = None,
                               fulfillment_status: Optional[Literal['claimed', 'fulfilled']] = None,
                               after: Optional[str] = None,
                               first: int = 200
                               ) -> Response[PData[List[activity.Entitlement]]]:
        params: Dict[str, Any] = {
            'id': entitlement_ids,
            'user_id': user_id,
            'game_id': game_id,
            'fulfillment_status': fulfillment_status.upper() if fulfillment_status is not None else None,
            'after': after,
            'first': first
        }
        return self.request(Route('GET', 'entitlements/drops', **params))

    def update_drops_entitlements(self,
                                  entitlement_ids: List[str],
                                  fulfillment_status: Literal['claimed', 'fulfilled']
                                  ) -> Response[Data[List[activity.EntitlementsUpdate]]]:
        params: Dict[str, Any] = {
            'entitlement_ids': entitlement_ids,
            'fulfillment_status': fulfillment_status.upper(),
        }
        return self.request(Route('PATCH', 'entitlements/drops', **params))

    # Games
    def get_top_games(self,
                      after: Optional[str] = None,
                      first: int = 20) -> Response[PData[List[search.Game]]]:

        params: Dict[str, Any] = {
            'first': first,
            'after': after,
        }
        return self.request(Route('GET', 'games/top', **params))

    def get_games(self,
                  game_ids: Optional[List[str]] = None,
                  names: Optional[List[str]] = None,
                  igdb_ids: Optional[List[str]] = None) -> Response[Data[List[search.Game]]]:
        params: Dict[str, Any] = {
            'id': game_ids,
            'name': names,
            'igdb_id': igdb_ids
        }
        return self.request(Route('GET', 'games', **params))

    # Goals
    def get_creator_goals(self, broadcaster_id: str) -> Response[Data[List[activity.Goal]]]:
        return self.request(Route('GET', 'goals', broadcaster_id=broadcaster_id))

    # Hype Train
    def get_hype_train_events(self,
                              broadcaster_id: str,
                              after: Optional[str] = None,
                              first: int = 20) -> Response[PData[List[interaction.HypeTrain]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'after': after,
            'first': first
        }
        return self.request(Route('GET', 'hypetrain/events', **params))

    # Moderation
    def check_automod_status(self,
                             broadcaster_id: str,
                             messages: List[str]
                             ) -> Response[Data[List[moderation.AutoModMessageStatus]]]:
        body = {'data': [{'msg_id': str(abs(hash(msg))), 'msg_text': msg} for msg in messages]}
        return self.request(
            Route('POST', 'moderation/enforcements/status', broadcaster_id=broadcaster_id),
            json=body
        )

    def manage_held_automod_messages(self,
                                     user_id: str,
                                     msg_id: str,
                                     action: Literal['allow', 'deny']
                                     ) -> Response[None]:
        params: Dict[str, Any] = {
            'user_id': user_id,
            'msg_id': msg_id,
            'action': action.upper()
        }
        return self.request(Route('POST', 'moderation/automod/message', **params))

    def get_automod_settings(self,
                             broadcaster_id: str,
                             moderator_id: str) -> Response[Data[List[moderation.AutoModSettings]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id,
        }
        return self.request(Route('GET', 'moderation/automod/settings', **params))

    def update_automod_settings(self,
                                broadcaster_id: str,
                                moderator_id: str,
                                *,
                                overall_level: Optional[int] = None,
                                aggression: Optional[int] = None,
                                bullying: Optional[int] = None,
                                disability: Optional[int] = None,
                                misogyny: Optional[int] = None,
                                race_ethnicity_or_religion: Optional[int] = None,
                                sex_based_terms: Optional[int] = None,
                                sexuality_sex_or_gender: Optional[int] = None,
                                swearing: Optional[int] = None
                                ) -> Response[Data[List[moderation.AutoModSettings]]]:

        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id,
        }

        if overall_level is None:
            body: Dict[str, Any] = {
                'aggression': aggression,
                'bullying': bullying,
                'disability': disability,
                'misogyny': misogyny,
                'race_ethnicity_or_religion': race_ethnicity_or_religion,
                'sex_based_terms': sex_based_terms,
                'sexuality_sex_or_gender': sexuality_sex_or_gender,
                'swearing': swearing}
            body = {key: value for key, value in body.items() if value is not None}
        else:
            body: Dict[str, Any] = {'overall_level': overall_level}
        return self.request(Route('PUT', 'moderation/automod/settings', **params), data=body)

    def get_banned_users(self,
                         broadcaster_id: str,
                         *,
                         user_ids: Optional[List[str]] = None,
                         first: int = 20,
                         before: Optional[str] = None,
                         after: Optional[str] = None) -> Response[PData[List[moderation.BannedUser]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'user_id': user_ids,
            'first': first,
            'before': before,
            'after': after,
        }

        return self.request(Route('GET', 'moderation/banned', **params))

    def ban_user(self,
                 broadcaster_id: str,
                 moderator_id: str,
                 user_id: str,
                 *,
                 duration: Optional[int] = None,
                 reason: Optional[str] = None) -> Response[Data[List[moderation.BanUser]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id,
        }

        body: Dict[str, Any] = {
            'user_id': user_id,
            'duration': duration,
            'reason': reason
        }
        body = {key: value for key, value in body.items() if value is not None}
        return self.request(Route('POST', 'moderation/bans', **params), json={'data': body})

    def unban_user(self,
                   broadcaster_id: str,
                   moderator_id: str,
                   user_id: str) -> Response[None]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id,
            'user_id': user_id
        }
        return self.request(Route('DELETE', 'moderation/bans', **params))

    def get_unban_requests(self,
                           broadcaster_id: str,
                           moderator_id: str,
                           status: Literal['pending', 'approved', 'denied', 'acknowledged', 'canceled'],
                           *,
                           user_id: Optional[str] = None,
                           after: Optional[str] = None,
                           first: int = 20
                           ) -> Response[PData[List[moderation.UnBanRequest]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id,
            'status': status,
            'user_id': user_id,
            'after': after,
            'first': first
        }

        return self.request(Route('GET', 'moderation/unban_requests', **params))

    def resolve_unban_requests(self,
                               broadcaster_id: str,
                               moderator_id: str,
                               *,
                               unban_request_id: str,
                               status: Literal['approved', 'denied'],
                               resolution_text: Optional[str] = None
                               ) -> Response[Data[List[moderation.UnBanRequest]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id,
            'unban_request_id': unban_request_id,
            'status': status,
            'resolution_text': resolution_text,
        }

        return self.request(Route('PATCH', 'moderation/unban_requests', **params))

    def get_blocked_terms(self,
                          broadcaster_id: str,
                          moderator_id: str,
                          first: int = 20,
                          after: Optional[str] = None
                          ) -> Response[PData[List[moderation.BlockedTerm]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id,
            'first': first,
            'after': after
        }

        return self.request(Route('GET', 'moderation/blocked_terms', **params))

    def add_blocked_term(self,
                         broadcaster_id: str,
                         moderator_id: str,
                         *,
                         text: str
                         ) -> Response[Data[List[moderation.BlockedTerm]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id
        }

        body: Dict[str, Any] = {
            'text': text
        }
        return self.request(Route('POST', 'moderation/blocked_terms', **params), data=body)

    def remove_blocked_term(self,
                            broadcaster_id: str,
                            moderator_id: str,
                            term_id: str
                            ) -> Response[None]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id,
            'id': term_id
        }
        return self.request(Route('DELETE', 'moderation/blocked_terms', **params))

    def delete_chat_messages(self,
                             broadcaster_id: str,
                             moderator_id: str,
                             *,
                             message_id: Optional[str] = None
                             ) -> Response[None]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id,
            'message_id': message_id
        }

        return self.request(Route('DELETE', 'moderation/chat', **params))

    def get_moderated_channels(self,
                               user_id: str,
                               after: Optional[str] = None,
                               first: int = 20
                               ) -> Response[PData[List[users.Broadcaster]]]:
        params: Dict[str, Any] = {
            'user_id': user_id,
            'after': after,
            'first': first
        }

        return self.request(Route('GET', 'moderation/channels', **params))

    def get_moderators(self,
                       broadcaster_id: str,
                       user_ids: Optional[List[str]] = None,
                       first: int = 20,
                       after: Optional[str] = None
                       ) -> Response[PData[List[users.SpecificUser]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'user_id': user_ids,
            'first': first,
            'after': after
        }
        return self.request(Route('GET', 'moderation/moderators', **params))

    def add_channel_moderator(self,
                              broadcaster_id: str,
                              user_id: str
                              ) -> Response[None]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'user_id': user_id
        }
        return self.request(Route('POST', 'moderation/moderators', **params))

    def remove_channel_moderator(self,
                                 broadcaster_id: str,
                                 user_id: str
                                 ) -> Response[None]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'user_id': user_id
        }
        return self.request(Route('DELETE', 'moderation/moderators', **params))

    def get_vips(self,
                 broadcaster_id: str,
                 user_ids: Optional[List[str]] = None,
                 first: int = 20,
                 after: Optional[str] = None
                 ) -> Response[PData[List[users.SpecificUser]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'user_id': user_ids,
            'first': first,
            'after': after
        }
        return self.request(Route('GET', 'channels/vips', **params))

    def add_channel_vip(self,
                        broadcaster_id: str,
                        user_id: str
                        ) -> Response[None]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'user_id': user_id
        }
        return self.request(Route('POST', 'channels/vips', **params))

    def remove_channel_vip(self,
                           broadcaster_id: str,
                           user_id: str
                           ) -> Response[None]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'user_id': user_id
        }
        return self.request(Route('DELETE', 'channels/vips', **params))

    def update_shield_mode_status(self,
                                  broadcaster_id: str,
                                  moderator_id: str,
                                  is_active: bool
                                  ) -> Response[Data[List[moderation.ShieldModeStatus]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id
        }
        data: Dict[str, Any] = {
            'is_active': is_active
        }
        return self.request(Route('PUT', 'moderation/shield_mode', **params), data=data)

    def get_shield_mode_status(self,
                               broadcaster_id: str,
                               moderator_id: str
                               ) -> Response[Data[List[moderation.ShieldModeStatus]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id
        }
        return self.request(Route('GET', 'moderation/shield_mode', **params))

    def warn_chat_user(self,
                       broadcaster_id: str,
                       moderator_id: str,
                       user_id: str,
                       reason: str
                       ) -> Response[Data[List[moderation.UserWarningResponse]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'moderator_id': moderator_id
        }
        body: Dict[str, Any] = {
            'user_id': user_id,
            'reason': reason
        }
        return self.request(Route('POST', 'moderation/warnings', **params), data=body)

    # Polls
    def get_polls(self,
                  broadcaster_id: str,
                  poll_ids: Optional[List[str]] = None,
                  first: int = 20,
                  after: Optional[str] = None
                  ) -> Response[PData[List[interaction.Poll]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'id': poll_ids,
            'first': first,
            'after': after
        }

        return self.request(Route('GET', 'polls', **params))

    def create_poll(self,
                    broadcaster_id: str,
                    title: str,
                    choices: List[str],
                    duration: int,
                    channel_points_voting_enabled: bool = False,
                    channel_points_per_vote: Optional[int] = None
                    ) -> Response[Data[List[interaction.Poll]]]:
        body: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'title': title,
            'choices': [{'title': choice} for choice in choices],
            'duration': duration,
            'channel_points_voting_enabled': channel_points_voting_enabled,
            'channel_points_per_vote': channel_points_per_vote
        }
        body = {key: value for key, value in body.items() if value is not None}
        return self.request(Route('POST', 'polls'), data=body)

    def end_poll(self,
                 broadcaster_id: str,
                 poll_id: str,
                 status: Literal['terminated', 'archived']
                 ) -> Response[Data[List[interaction.Poll]]]:
        body: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'id': poll_id,
            'status': status.upper()
        }
        return self.request(Route('PATCH', 'polls'), data=body)

    # Predictions
    def get_predictions(self,
                        broadcaster_id: str,
                        prediction_ids: Optional[List[str]] = None,
                        first: int = 20,
                        after: Optional[str] = None) -> Response[PData[List[interaction.Prediction]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'id': prediction_ids,
            'first': first,
            'after': after
        }
        return self.request(Route('GET', 'predictions', **params))

    def create_prediction(self,
                          broadcaster_id: str,
                          title: str,
                          outcomes: List[str],
                          prediction_window: int
                          ) -> Response[Data[List[interaction.Prediction]]]:
        body: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'title': title,
            'outcomes': [{'title': outcome} for outcome in outcomes],
            'prediction_window': prediction_window
        }
        return self.request(Route('POST', 'predictions'), data=body)

    def end_prediction(self,
                       broadcaster_id: str,
                       prediction_id: str,
                       status: Literal['resolved', 'canceled', 'locked'],
                       winning_outcome_id: Optional[str] = None
                       ) -> Response[Data[List[interaction.Prediction]]]:
        body: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'id': prediction_id,
            'status': status.upper(),
            'winning_outcome_id': winning_outcome_id
        }
        body = {key: value for key, value in body.items() if value is not None}
        return self.request(Route('PATCH', 'predictions'), data=body)

    # Raid
    def start_raid(self,
                   from_broadcaster_id: str,
                   to_broadcaster_id: str
                   ) -> Response[Data[List[streams.RaidInfo]]]:
        body: Dict[str, Any] = {
            'from_broadcaster_id': from_broadcaster_id,
            'to_broadcaster_id': to_broadcaster_id
        }
        return self.request(Route('POST', 'raids'), data=body)

    def cancel_raid(self,
                    broadcaster_id: str
                    ) -> Response[None]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id
        }
        return self.request(Route('DELETE', 'raids', **params))

    def get_channel_stream_schedule(self,
                                    broadcaster_id: str,
                                    segment_ids: Optional[List[str]] = None,
                                    start_time: Optional[str] = None,
                                    first: int = 20,
                                    after: Optional[str] = None
                                    ) -> Response[PData[List[streams.Schedule]]]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'id': segment_ids,
            'start_time': start_time,
            'first': first,
            'after': after
        }

        return self.request(Route('GET', 'schedule', **params))

    def get_channel_icalendar(self, broadcaster_id: str) -> Response[str]:
        return self.request(Route('GET', 'schedule/icalendar', broadcaster_id=broadcaster_id))

    def update_channel_stream_schedule(self,
                                       broadcaster_id: str,
                                       is_vacation_enabled: bool,
                                       vacation_start_time: Optional[str] = None,
                                       vacation_end_time: Optional[str] = None,
                                       timezone: Optional[str] = None
                                       ) -> Response[None]:
        params: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'is_vacation_enabled': is_vacation_enabled,
            'vacation_start_time': vacation_start_time,
            'vacation_end_time': vacation_end_time,
            'timezone': timezone
        }
        return self.request(Route('PATCH', 'schedule/settings', **params))

    def create_channel_schedule_segment(self,
                                        broadcaster_id: str,
                                        start_time: str,
                                        timezone: str,
                                        duration: int,
                                        is_recurring: Optional[bool] = None,
                                        category_id: Optional[str] = None,
                                        title: Optional[str] = None
                                        ) -> Response[Data[List[streams.Schedule]]]:
        body: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'start_time': start_time,
            'timezone': timezone,
            'duration': str(duration),
            'is_recurring': is_recurring,
            'category_id': category_id,
            'title': title
        }
        body = {key: value for key, value in body.items() if value is not None}
        return self.request(Route('POST', 'schedule/segment'), data=body)

    def update_channel_schedule_segment(self,
                                        broadcaster_id: str,
                                        segment_id: str,
                                        start_time: Optional[str] = None,
                                        duration: Optional[int] = None,
                                        category_id: Optional[str] = None,
                                        title: Optional[str] = None,
                                        is_canceled: Optional[bool] = None,
                                        timezone: Optional[str] = None
                                        ) -> Response[Data[List[streams.Schedule]]]:
        body: Dict[str, Any] = {
            'broadcaster_id': broadcaster_id,
            'id': segment_id,
            'start_time': start_time,
            'duration': str(duration),
            'category_id': category_id,
            'title': title,
            'is_canceled': is_canceled,
            'timezone': timezone
        }
        body = {key: value for key, value in body.items() if value is not None}
        return self.request(Route('PATCH', 'schedule/segment'), data=body)

    def delete_channel_schedule_segment(self,
                                        broadcaster_id: str,
                                        segment_id: str
                                        ) -> Response[None]:
        params = {
            'broadcaster_id': broadcaster_id,
            'id': segment_id
        }
        return self.request(Route('DELETE', 'schedule/segment', **params))

    # Search
    def search_categories(self,
                          query: str,
                          first: int = 20,
                          after: Optional[str] = None
                          ) -> Response[PData[List[search.CategorySearch]]]:
        params = {
            'query': query,
            'first': first,
            'after': after
        }
        return self.request(Route('GET', 'search/categories', **params))

    def search_channels(self,
                        query: str,
                        live_only: bool = False,
                        first: int = 20,
                        after: Optional[str] = None
                        ) -> Response[PData[List[search.ChannelSearch]]]:
        params = {
            'query': query,
            'live_only': live_only,
            'first': first,
            'after': after
        }
        return self.request(Route('GET', 'search/channels', **params))

    # Streams
    def get_stream_key(self, broadcaster_id: str) -> Response[Data[List[streams.StreamKey]]]:
        return self.request(Route('GET', 'streams/key', broadcaster_id=broadcaster_id))

    def get_streams(self,
                    user_ids: Optional[List[str]] = None,
                    user_logins: Optional[List[str]] = None,
                    game_ids: Optional[List[str]] = None,
                    stream_type: Literal['all', 'live'] = 'all',
                    language: Optional[str] = None,
                    first: int = 20,
                    before: Optional[str] = None,
                    after: Optional[str] = None
                    ) -> Response[PData[List[streams.StreamInfo]]]:
        params = {
            'user_id': user_ids,
            'user_login': user_logins,
            'game_id': game_ids,
            'type': stream_type,
            'language': language,
            'first': first,
            'before': before,
            'after': after
        }
        return self.request(Route('GET', 'streams', **params))

    def get_followed_streams(self,
                             user_id: str,
                             first: int = 100,
                             after: Optional[str] = None
                             ) -> Response[PData[List[streams.StreamInfo]]]:
        params = {
            'user_id': user_id,
            'first': first,
            'after': after
        }
        return self.request(Route('GET', 'streams/followed', **params))

    def create_stream_marker(self,
                             user_id: str,
                             description: Optional[str] = None
                             ) -> Response[Data[List[streams.StreamMarkerInfo]]]:
        body: Dict[str, Any] = {
            'user_id': user_id,
            'description': description
        }
        return self.request(Route('POST', 'streams/markers'), data=body)

    def get_stream_markers(self,
                           user_id: [str] = None,
                           video_id: Optional[str] = None,
                           first: Optional[int] = 20,
                           before: Optional[str] = None,
                           after: Optional[str] = None
                           ) -> Response[PData[List[streams.StreamMarker]]]:
        params = {
            'user_id': user_id,
            'video_id': video_id,
            'first': first,
            'before': before,
            'after': after
        }
        return self.request(Route('GET', 'streams/markers', **params))

    # Subscriptions
    def get_broadcaster_subscriptions(self,
                                      broadcaster_id: str,
                                      user_ids: Optional[List[str]] = None,
                                      first: Optional[int] = 20,
                                      before: Optional[str] = None,
                                      after: Optional[str] = None
                                      ) -> Response[TPData[List[channels.Subscription]]]:
        params = {
            'broadcaster_id': broadcaster_id,
            'user_id': user_ids,
            'first': first,
            'before': before,
            'after': after
        }
        return self.request(Route('GET', 'subscriptions', **params))

    def check_user_subscription(self,
                                user_id: str,
                                broadcaster_id: str
                                ) -> Response[Data[List[channels.SubscriptionCheck]]]:
        params = {
            'broadcaster_id': broadcaster_id,
            'user_id': user_id
        }
        return self.request(Route('GET', 'subscriptions/user', **params))

    def get_team_info(self,
                      team_name: Optional[str] = None,
                      team_id: Optional[str] = None
                      ) -> Response[Data[List[channels.Team]]]:
        params = {
            'name': team_name.replace(' ', '').lower(),
            'id': team_id
        }
        return self.request(Route('GET', 'teams', **params))

    def get_channel_teams(self, broadcaster_id: str) -> Response[Data[List[channels.ChannelTeam]]]:
        return self.request(Route('GET', 'teams/channel', broadcaster_id=broadcaster_id))

    # Users
    def get_users(self,
                  user_ids: Optional[List[str]] = None,
                  user_logins: Optional[List[str]] = None) -> Response[Data[List[users.User]]]:
        params = {
            'login': user_logins,
            'id': user_ids
        }
        return self.request(Route('GET', 'users', **params))

    def update_user(self, description: str) -> Response[Data[List[users.User]]]:
        return self.request(Route('PUT', 'users', description=description))

    def get_user_block_list(self,
                            broadcaster_id: str,
                            first: int = 20,
                            after: str = None
                            ) -> Response[PData[List[users.SpecificUser]]]:
        params = {
            'broadcaster_id': broadcaster_id,
            'first': first,
            'after': after
        }
        return self.request(Route('GET', 'users/blocks', **params))

    def block_user(self, user_id: str,
                   source_context: Literal['chat', 'whisper'] = None,
                   reason: Literal['harassment', 'spam', 'other'] = None) -> Response[None]:
        params = {
            'target_user_id': user_id,
            'source_context': source_context,
            'reason': reason
        }
        return self.request(Route('PUT', 'users/blocks', **params))

    def unblock_user(self, user_id: str) -> Response[None]:
        return self.request(Route('DELETE', 'users/blocks', user_id=user_id))

    def get_user_extensions(self) -> Response[Data[List[channels.Extension]]]:
        return self.request(Route('GET', 'users/extensions/list'))

    def get_user_active_extensions(self,
                                   user_id: Optional[str] = None
                                   ) -> Response[Data[channels.ActiveExtensions]]:
        return self.request(Route('GET', 'users/extensions', user_id=user_id))

    def update_user_extensions(self,
                               key: Literal['panel', 'overlay', 'component'],
                               number: Literal['1', '2', '3'],
                               extension_id: str,
                               extension_version: str,
                               activate: bool,
                               x: Optional[int] = None,
                               y: Optional[int] = None) -> Response[Data[channels.ActiveExtensions]]:
        body = {
            'data': {str(key): {number: {'id': extension_id,
                                         'version': extension_version,
                                         'active': activate}}}
        }
        if x and y:
            body[key][number].update({'x': x, 'y': y})
        return self.request(Route('PUT', 'users/extensions'), data=body)

    # Videos
    def get_videos(self,
                   video_ids: Optional[List[str]] = None,
                   user_id: Optional[str] = None,
                   game_id: Optional[str] = None,
                   language: Optional[str] = None,
                   period: Optional[Literal['all', 'day', 'month', 'week']] = None,
                   sort: Optional[Literal['time', 'trending', 'views']] = None,
                   video_type: Optional[Literal['all', 'archive', 'highlight', 'upload']] = None,
                   first: Optional[int] = 20,
                   after: Optional[str] = None,
                   before: Optional[str] = None
                   ) -> Response[PData[List[channels.Video]]]:
        params: Dict[str, Any] = {
            'id': video_ids,
            'user_id': user_id,
            'game_id': game_id,
            'language': language,
            'period': period,
            'sort': sort,
            'type': video_type,
            'first': first,
            'after': after,
            'before': before
        }
        return self.request(Route('GET', 'videos', **params))

    def delete_videos(self, video_ids: List[str]) -> Response[Data[List[str]]]:
        return self.request(Route('DELETE', 'videos', id=video_ids))

    # Whispers
    def send_whisper(self,
                     from_user_id: str,
                     to_user_id: str,
                     message: str
                     ) -> Response[None]:
        params: Dict[str, Any] = {
            'from_user_id': from_user_id,
            'to_user_id': to_user_id,
        }
        body: Dict[str, Any] = {
            'message': message
        }
        return self.request(Route('POST', 'whispers', **params), data=body)
