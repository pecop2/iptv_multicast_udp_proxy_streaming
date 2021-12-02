# IPTV Multicast UDP Proxy streaming

# Idea
Most IPTV providers support only one client at the same time, so target is, using VLC players to buffer the stream serving multiple clients at the same time. When watching the same stream, N clients can be served without problems. When the streams are different, depends on network connection and stream quality. Short video repeats when serving different streams are possible due to change of streams in the background.

- Local file server started for the newly created m3u list
- Local udp proxy server
- Using VLC python library
- M3U list gets updated every 12 hours

# Environment variable config
- **ORIGINAL_M3U_URL** : the url provided from your IPTV provider
- **NUMBER_OF_CLIENTS** : the expected number of clients in order to pre-start the vlc players (no problem if it is lower than actual, maybe slightly slower response for the additional clients)
- **HOST_IP** : the local IP address of the machine running this

# Multicast
- Not every network is multicast ready (enable IGMP if an option), so beware or specifically add routes for loopback of multicast traffic if your network gets flooded (if the router/switch treats it as broadcast)

# Docker build and run

- Build and run
    - **docker-compose up -d --build**
- File server for the newly generated m3u file runs on port: **8010**
- The simple udp proxy runs on port: **8011**
- Before starting the service, change the environment variables in the **docker-compose.yml** to your own values.
- Please don't change host:container ports, it wouldn't work with different values
- Download the new generated m3u list from: **http://your_host_ip:8010/channels_multicast.m3u** (your_host_ip should be the same with the environment variable **HOST_IP**)

