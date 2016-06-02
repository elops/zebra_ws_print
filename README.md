# WebSocket server for LinkOS enabled printers to connect to

Capabilities: 

 - opening main communication channel
 - opening secondary config or raw channel
 - sending configuration or print tasks to secondary channels

Requirements

 - python3.5 and websockets module (requirements.txt)

Zebra related documentation

 - Creating Weblink endpoint
   https://www.zebra.com/content/dam/zebra/software/en/application-notes/appnotes-creating-weblink-endpoint-rev1-english.pdf

 - ZPL / ZBI2 programming manual
   https://www.zebra.com/content/dam/zebra/manuals/en-us/software/zpl-zbi2-pm-en.pdf



Zebra Weblink behavior odities

 - Once printer connects to websocket server it does not conclude that it is connected and should stop connecting again.
   Only way I've found to prevent it to connect again to main channel was to limit number of weblink connections to 2

   SGD JSON syntax:
   {}{"weblink.ip.conn1.maximum_simultaneous_connections":"2"}

   After this printer restart is required for new settings to take effect.

   Once printer reboots and connects to main channel, you likely want to open 'raw' channel and that totals 2 connections.
   When printer reaches limit of connections we defined it stops from trying to connect every 'weblink.ip.conn1.retry_interval'
   seconds

   SGD JSON to adjust retry interval
   {}{"weblink.ip.conn1.retry_interval":"10"}

   When this was adjusted printer - server websocket connections were not breaking/failing until after 60 seconds.
   In this demo I used NGiNX to handle the SSL and proxy websocket to local TCP port where our websocket server was running.
   NGiNX suspends inactive long lived connections after 'proxy_read_timeout' seconds which is by default set to 60 seconds.
   Zebra printer and websocket server exchange websocket.ping and websocket.pong every ~60 sec which prevents connections 
   from going inactive, but still default 'proxy_read_timeout' set at 60 is too low so I had to increase it.
   Once I've bumped 'proxy_read_timeout' to 120 seconds there were no more connection outages (code: 1006)

   Different situation is if you attempt to open configuration channel however. This channel closes as soon as it receives SGD command
   with websocket code 1000. I'm still puzzled how to ocassionally open configuration channel and yet prevent printer to reconnect
   when configuration channel is not used because of the limit 'weblink.ip.conn1.maximum_simultaneous_connections' which ought to be
   set to number of channels one intends to use, and with config channel being opened for only a short period of time it seems impossible
   to prevent printer from connecting again.


