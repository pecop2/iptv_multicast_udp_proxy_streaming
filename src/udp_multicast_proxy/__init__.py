"""Share IPTV channels between many players.

The provider's playlist is rewritten so that every channel points at a local HTTP
proxy. When a viewer opens a channel, ffmpeg restreams it to a UDP multicast group
(once, no matter how many viewers there are) and the proxy relays the multicast
MPEG-TS to each viewer over HTTP.
"""
