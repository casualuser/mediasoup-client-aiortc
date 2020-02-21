from typing import Any, Dict, Optional
import base64
import asyncio
from aiortc import (
    RTCConfiguration,
    RTCDataChannel,
    RTCPeerConnection,
    RTCRtpTransceiver,
    RTCSessionDescription,
    RTCStatsReport
)

from channel import Request, Notification, Channel
from logger import Logger


class Handler:
    def __init__(
        self,
        handlerId: str,
        channel: Channel,
        loop: asyncio.AbstractEventLoop,
        getTrack,
        configuration: Optional[RTCConfiguration] = None
    ) -> None:
        self._handlerId = handlerId
        self._channel = channel
        self._pc = RTCPeerConnection(configuration or None)
        # dictionary of transceivers mapped by track id
        self._transceivers = dict()  # type: Dict[str, RTCRtpTransceiver]
        # dictionary of dataChannelds mapped by internal id
        self._dataChannels = dict()  # type: Dict[str, RTCDataChannel]
        # function returning a track given a player ID and a kind
        self._getTrack = getTrack

        @self._pc.on("track")  # type: ignore
        def on_track(track) -> None:
            Logger.debug(f"handler: ontrack [kind:{track.kind}, id:{track.id}]")

        @self._pc.on("signalingstatechange")  # type: ignore
        async def on_signalingstatechange() -> None:
            Logger.debug(
                f"handler: signalingstatechange [state:{self._pc.signalingState}]"
            )
            await self._channel.notify(
                self._handlerId,
                "signalingstatechange",
                self._pc.signalingState
            )

        @self._pc.on("icegatheringstatechange")  # type: ignore
        async def on_icegatheringstatechange() -> None:
            Logger.debug(
                f"handler: icegatheringstatechange [state:{self._pc.iceGatheringState}]"
            )
            await self._channel.notify(
                self._handlerId,
                "icegatheringstatechange",
                self._pc.iceGatheringState
            )

        @self._pc.on("iceconnectionstatechange")  # type: ignore
        async def on_iceconnectionstatechange() -> None:
            Logger.debug(
                f"handler: iceconnectionstatechange [state:{self._pc.iceConnectionState}]"
            )
            await self._channel.notify(
                self._handlerId,
                "iceconnectionstatechange",
                self._pc.iceConnectionState
            )

        async def checkDataChannelsBufferedAmount() -> None:
            while True:
                await asyncio.sleep(1)
                for dataChannelId, dataChannel in self._dataChannels.items():
                    await self._channel.notify(dataChannelId, "bufferedamount", dataChannel.bufferedAmount)

        self._dataChannelsBufferedAmountTask = loop.create_task(
            checkDataChannelsBufferedAmount()
        )

    async def close(self) -> None:
        # stop the periodic task
        self._dataChannelsBufferedAmountTask.cancel()

        # close peerconnection
        await self._pc.close()

    async def processRequest(self, request: Request) -> Any:
        if request.method == "handler.getLocalDescription":
            localDescription = self._pc.localDescription
            result = None

            if (localDescription is not None):
                result = {}
                result["type"] = localDescription.type
                result["sdp"] = localDescription.sdp
                return result

        elif request.method == "handler.addTrack":
            data = request.data
            playerId = data["playerId"]
            kind = data["kind"]
            track = self._getTrack(playerId, kind)
            transceiver = self._pc.addTransceiver(track)

            # store transceiver in the dictionary
            self._transceivers[track.id] = transceiver

            result = {}
            result["trackId"] = track.id
            return result

        elif request.method == "handler.removeTrack":
            data = request.data
            trackId = data.get("trackId")
            if trackId is None:
                raise TypeError("missing trackId")

            transceiver = self._transceivers[trackId]
            transceiver.direction = "inactive"
            transceiver.sender.replaceTrack(None)

            # remove transceiver from the dictionary
            del self._transceivers[trackId]

        elif request.method == "handler.setLocalDescription":
            data = request.data
            if isinstance(data, RTCSessionDescription):
                raise TypeError("request data not a RTCSessionDescription")

            description = RTCSessionDescription(**data)
            await self._pc.setLocalDescription(description)

        elif request.method == "handler.setRemoteDescription":
            data = request.data
            if isinstance(data, RTCSessionDescription):
                raise TypeError("request data not a RTCSessionDescription")

            description = RTCSessionDescription(**data)
            await self._pc.setRemoteDescription(description)

        elif request.method == "handler.createOffer":
            offer = await self._pc.createOffer()
            result = {}
            result["type"] = offer.type
            result["sdp"] = offer.sdp
            return result

        elif request.method == "handler.createAnswer":
            answer = await self._pc.createAnswer()
            result = {}
            result["type"] = answer.type
            result["sdp"] = answer.sdp
            return result

        elif request.method == "handler.getMid":
            data = request.data
            trackId = data.get("trackId")
            if trackId is None:
                raise TypeError("missing trackId")

            # raise on purpose if the key is not found
            transceiver = self._transceivers[trackId]
            return transceiver.mid

        elif request.method == "handler.getTransportStats":
            result = {}
            stats = await self._pc.getStats()
            for key in stats:
                type = stats[key].type
                if type == "inbound-rtp":
                    result[key] = self._serializeInboundStats(stats[key])
                elif type == "outbound-rtp":
                    result[key] = self._serializeOutboundStats(stats[key])
                elif type == "remote-inbound-rtp":
                    result[key] = self._serializeRemoteInboundStats(stats[key])
                elif type == "remote-outbound-rtp":
                    result[key] = self._serializeRemoteOutboundStats(stats[key])
                elif type == "transport":
                    result[key] = self._serializeTransportStats(stats[key])

            return result

        elif request.method == "handler.getSenderStats":
            data = request.data
            mid = data.get("mid")
            if mid is None:
                raise TypeError("missing mid")

            transceiver = self._getTransceiverByMid(mid)
            sender = transceiver.sender
            result = {}
            stats = await sender.getStats()
            for key in stats:
                type = stats[key].type
                if type == "outbound-rtp":
                    result[key] = self._serializeOutboundStats(stats[key])
                elif type == "remote-inbound-rtp":
                    result[key] = self._serializeRemoteInboundStats(stats[key])
                elif type == "transport":
                    result[key] = self._serializeTransportStats(stats[key])

            return result

        elif request.method == "handler.getReceiverStats":
            data = request.data
            mid = data.get("mid")
            if mid is None:
                raise TypeError("missing mid")

            transceiver = self._getTransceiverByMid(mid)
            receiver = transceiver.receiver
            result = {}
            stats = await receiver.getStats()
            for key in stats:
                type = stats[key].type
                if type == "inbound-rtp":
                    result[key] = self._serializeInboundStats(stats[key])
                elif type == "remote-outbound-rtp":
                    result[key] = self._serializeRemoteOutboundStats(stats[key])
                elif type == "transport":
                    result[key] = self._serializeTransportStats(stats[key])

            return result

        elif request.method == "handler.createDataChannel":
            internal = request.internal
            dataChannelId = internal.get("dataChannelId")
            data = request.data
            id = data.get("id")
            ordered = data.get("ordered")
            maxPacketLifeTime = data.get("maxPacketLifeTime")
            maxRetransmits = data.get("maxRetransmits")
            label = data.get("label")
            protocol = data.get("protocol")
            dataChannel = self._pc.createDataChannel(
                negotiated=True,
                id=id,
                ordered=ordered,
                maxPacketLifeTime=maxPacketLifeTime,
                maxRetransmits=maxRetransmits,
                label=label,
                protocol=protocol
            )

            # store datachannel in the dictionary
            self._dataChannels[dataChannelId] = dataChannel

            @dataChannel.on("open")  # type: ignore
            async def on_open() -> None:
                await self._channel.notify(dataChannelId, "open")

            @dataChannel.on("closing")  # type: ignore
            async def on_closing() -> None:
                await self._channel.notify(dataChannelId, "closing")

            @dataChannel.on("close")  # type: ignore
            async def on_close() -> None:
                # NOTE: After calling dataChannel.close() aiortc emits "close" event
                # on the dataChannel. Probably it shouldn't do it. So caution.
                try:
                    del self._dataChannels[dataChannelId]
                    await self._channel.notify(dataChannelId, "close")
                except KeyError:
                    pass

            @dataChannel.on("message")  # type: ignore
            async def on_message(message) -> None:
                if isinstance(message, str):
                    await self._channel.notify(dataChannelId, "message", message)
                if isinstance(message, bytes):
                    message_bytes = base64.b64encode(message)
                    await self._channel.notify(
                        dataChannelId, "binary", str(message_bytes))

            @dataChannel.on("bufferedamountlow")  # type: ignore
            async def on_bufferedamountlow() -> None:
                await self._channel.notify(dataChannelId, "bufferedamountlow")

            return {
                "streamId": dataChannel.id,
                "ordered": dataChannel.ordered,
                "maxPacketLifeTime": dataChannel.maxPacketLifeTime,
                "maxRetransmits": dataChannel.maxRetransmits,
                "label": dataChannel.label,
                "protocol": dataChannel.protocol,
                # status fields
                "readyState": dataChannel.readyState,
                "bufferedAmount": dataChannel.bufferedAmount,
                "bufferedAmountLowThreshold": dataChannel.bufferedAmountLowThreshold
            }

        else:
            raise TypeError(
                f"unknown request with method '{request.method}' received"
            )

    async def processNotification(self, notification: Notification) -> None:
        if notification.event == "enableTrack":
            Logger.warning("handler: enabling track not implemented")

        elif notification.event == "disableTrack":
            Logger.warning("handler: disabling track not implemented")

        elif notification.event == "datachannel.send":
            internal = notification.internal
            dataChannelId = internal.get("dataChannelId")
            if dataChannelId is None:
                raise TypeError("missing dataChannelId")

            data = notification.data
            dataChannel = self._dataChannels[dataChannelId]
            dataChannel.send(data)

            # Good moment to update bufferedAmount in Node.js side
            await self._channel.notify(
                dataChannelId, "bufferedamount", dataChannel.bufferedAmount
            )

        elif notification.event == "datachannel.sendBinary":
            internal = notification.internal
            dataChannelId = internal.get("dataChannelId")
            if dataChannelId is None:
                raise TypeError("missing dataChannelId")

            data = notification.data
            dataChannel = self._dataChannels[dataChannelId]
            dataChannel.send(base64.b64decode(data))

            # Good moment to update bufferedAmount in Node.js side
            await self._channel.notify(
                dataChannelId, "bufferedamount", dataChannel.bufferedAmount
            )

        elif notification.event == "datachannel.close":
            internal = notification.internal
            dataChannelId = internal.get("dataChannelId")
            if dataChannelId is None:
                raise TypeError("missing dataChannelId")
            dataChannel = self._dataChannels.get(dataChannelId)
            if dataChannel is None:
                return

            # NOTE: After calling dataChannel.close() aiortc emits "close" event
            # on the dataChannel. Probably it shouldn't do it. So caution.
            try:
                del self._dataChannels[dataChannelId]
            except KeyError:
                pass

            dataChannel.close()

        elif notification.event == "datachannel.setBufferedAmountLowThreshold":
            internal = notification.internal
            dataChannelId = internal.get("dataChannelId")
            if dataChannelId is None:
                raise TypeError("missing dataChannelId")

            value = notification.data
            dataChannel = self._dataChannels[dataChannelId]
            dataChannel.bufferedAmountLowThreshold = value

        else:
            raise TypeError(
                f"unknown notification with event '{notification.event}' received"
            )

    """
    Helper functions
    """

    def _getTransceiverByMid(self, mid: str) -> Optional[RTCRtpTransceiver]:
        return next(
            filter(lambda x: x.mid == mid, self._pc.getTransceivers()), None
        )

    def _serializeInboundStats(self, stats: RTCStatsReport) -> Dict[str, Any]:
        return {
            # RTCStats
            "timestamp": stats.timestamp.timestamp(),
            "type": stats.type,
            "id": stats.id,
            # RTCStreamStats
            "ssrc": stats.ssrc,
            "kind": stats.kind,
            "transportId": stats.transportId,
            # RTCReceivedRtpStreamStats
            "packetsReceived": stats.packetsReceived,
            "packetsLost": stats.packetsLost,
            "jitter": stats.jitter
        }

    def _serializeOutboundStats(self, stats: RTCStatsReport) -> Dict[str, Any]:
        return {
            # RTCStats
            "timestamp": stats.timestamp.timestamp(),
            "type": stats.type,
            "id": stats.id,
            # RTCStreamStats
            "ssrc": stats.ssrc,
            "kind": stats.kind,
            "transportId": stats.transportId,
            # RTCSentRtpStreamStats
            "packetsSent": stats.packetsSent,
            "bytesSent": stats.bytesSent,
            # RTCOutboundRtpStreamStats
            "trackId": stats.trackId
        }

    def _serializeRemoteInboundStats(self, stats: RTCStatsReport) -> Dict[str, Any]:
        return {
            # RTCStats
            "timestamp": stats.timestamp.timestamp(),
            "type": stats.type,
            "id": stats.id,
            # RTCStreamStats
            "ssrc": stats.ssrc,
            "kind": stats.kind,
            "transportId": stats.transportId,
            # RTCReceivedRtpStreamStats
            "packetsReceived": stats.packetsReceived,
            "packetsLost": stats.packetsLost,
            "jitter": stats.jitter,
            # RTCRemoteInboundRtpStreamStats
            "roundTripTime": stats.roundTripTime,
            "fractionLost": stats.fractionLost
        }

    def _serializeRemoteOutboundStats(self, stats: RTCStatsReport) -> Dict[str, Any]:
        return {
            # RTCStats
            "timestamp": stats.timestamp.timestamp(),
            "type": stats.type,
            "id": stats.id,
            # RTCStreamStats
            "ssrc": stats.ssrc,
            "kind": stats.kind,
            "transportId": stats.transportId,
            # RTCSentRtpStreamStats
            "packetsSent": stats.packetsSent,
            "bytesSent": stats.bytesSent,
            # RTCRemoteOutboundRtpStreamStats
            "remoteTimestamp": stats.remoteTimestamp.timestamp()
        }

    def _serializeTransportStats(self, stats: RTCStatsReport) -> Dict[str, Any]:
        return {
            # RTCStats
            "timestamp": stats.timestamp.timestamp(),
            "type": stats.type,
            "id": stats.id,
            # RTCTransportStats
            "packetsSent": stats.packetsSent,
            "packetsReceived": stats.packetsReceived,
            "bytesSent": stats.bytesSent,
            "bytesReceived": stats.bytesReceived,
            "iceRole": stats.iceRole,
            "dtlsState": stats.dtlsState
        }
