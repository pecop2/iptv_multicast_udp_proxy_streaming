# iptv_multicast_udp_proxy_streaming



### Docker build and run

- Build and run
    - **docker-compose up -d --build**
- File server for the newly generated m3u file runs on port: **8010**
- The simple udp proxy runs on port: **8011**
- Before starting the service, change the environment variables in the **docker-compose.yml** to your own values.
- Please don't change host:container ports, it wouldn't work with different values

