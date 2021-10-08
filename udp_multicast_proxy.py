import socket
import struct
import sys
import threading
import time
import binascii
import os
from http import HTTPStatus
import http.server
import socketserver
import requests 
import queue
import vlc
from tqdm import tqdm
import pickle
from socket import timeout
import shutil
import platform


class MulticastStream(threading.Thread):
    def __init__(self, multicast_address):
        global mcast_url_map

        super(MulticastStream, self).__init__()

        self.multicast_address = multicast_address

        mcast_url_map_lock.acquire()
        self.URL = mcast_url_map[self.multicast_address]
        mcast_url_map_lock.release()
    
    def generate_cmd(self):
		# vlc_path + URL + mapped_channel_id + vlc_output_string
        # vlc_path = "C:\\Program Files\\VideoLAN\\VLC\\vlc.exe"
        vlc_output_string_cmd = "sout=#rtp{dst=" + self.multicast_address + ",port=5004,mux=ts} sout-all sout-keep"
        cmd_str = self.URL + " " + vlc_output_string_cmd

        print (cmd_str)

        return cmd_str.split()

    def run(self):
        print ("starting..." + self.multicast_address)

        vlc_media_cmd = self.generate_cmd()

        global vlc_players_q

        try:
            vlc_player = vlc_players_q.get_nowait()
        except Exception as e:
            vlc_player = vlc.MediaListPlayer()

        vlc_media = vlc.Media(*vlc_media_cmd)
        vlc_media_list = vlc.MediaList()
        vlc_media_list.add_media(vlc_media)
        vlc_player.set_media_list(vlc_media_list)
        
        vlc_player.play()
        vlc_player.set_playback_mode(vlc.PlaybackMode.repeat)

        global vlc_players_dict
        
        vlc_players_dict[self.multicast_address] = (vlc_player, 1)
        vlc_players_lock.release()

        # print (vlc_media_cmd)

def process_server_request(command, multicast_address):
    vlc_players_lock.acquire()

    if command == "START":
        start_multicast_player(multicast_address)
    elif command == "STOP":
        stop_multicast_player(multicast_address)

def start_multicast_player(multicast_address):
    
    if multicast_address in vlc_players_dict:
        old_value = vlc_players_dict[multicast_address]
        new_value = (old_value[0], old_value[1] + 1)
        vlc_players_dict[multicast_address] = new_value
        vlc_players_lock.release()
    else:
        vlc_players_dict[multicast_address] = (None, 1) # da se popolni dictot, za slucajno nareden thread da ne pushti nov player za istiot stream...
        multicast_stream = MulticastStream(multicast_address)
        multicast_stream.start()

def stop_multicast_player(multicast_address):
    if multicast_address in vlc_players_dict: # should always be true
        old_value = vlc_players_dict[multicast_address]

        number_of_viewers = old_value[1]

        if number_of_viewers == 1: # Last viewer is leaving, stop the vlc_player completely
            vlc_player_to_stop = old_value[0]
            vlc_player_to_stop.stop()
            vlc_player_to_stop.release()
            vlc_players_q.put(vlc.MediaListPlayer())

            del vlc_players_dict[multicast_address]
        else: # namali number of viewers
            vlc_players_dict[multicast_address] = ( old_value[0], old_value[1] - 1 )
    vlc_players_lock.release()
            
def get_host_name_IP(): 

    # host_name_ip = ""
    # try: 
    #     s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    #     s.connect(("8.8.8.8", 80))
    #     host_name_ip = s.getsockname()[0]
    #     s.close()
    #     # print ("Host ip:", host_name_ip)
    #     return host_name_ip
    # except: 
    #     print("Unable to get Hostname") 
    return os.environ['HOST_IP']

def write_data_stream(data_buffer, data_stream, packet_count, queue):
    whole_data=bytes()
    mul = 0

    for mul in range(int(len(data_buffer) / packet_count)):
        # print ("mul=", mul)
        for data in data_buffer[mul*packet_count:(mul+1)*packet_count]:
            whole_data += data
        try:
            data_stream.write(whole_data)
            whole_data = bytes()
        except OSError as err:
            queue.put(err)
            return

def write_to_disk(mcast_address, data):
    # if not os.path.exists("disk_buffer"):
    disk_buffer_dir = "disk_buffer"
    os.makedirs(name = disk_buffer_dir, exist_ok=True)

    vid_name = os.path.join(disk_buffer_dir, mcast_address) + ".mp4"
    
    with open(vid_name, "ab") as vid_file:

        vid_file.write(data)
        # vid_file.r
    
    update_last_written_data_map(mcast_address, data)

def update_last_written_data_map(mcast_address, data):
    last_written_data_lock.acquire()

    last_written_data_map[mcast_address] = data

    last_written_data_lock.release()



class StreamHandler(http.server.BaseHTTPRequestHandler):
    # def __init__(self, *args, **kwargs):
    #     super().__init__(*args, **kwargs)

    def do_GET(self):

        # content_length = int(self.headers['Content-Length'])
        # post_body = self.rfile.read(content_length)
        exception_queue = queue.Queue()

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-type", "application/octet-stream")
        # self.send_header("Content-type", "video/mp4")
        self.end_headers()

        mcast_address_port = self.path.split("/")[-1].split(":")
        mcast_address = mcast_address_port[0]
        mcast_port = int(mcast_address_port[1])

        # here the stream should be started
        threading.Thread(target = process_server_request, args = ("START", mcast_address) ).start() #dont forget to UNcomment it later

        recv_socket = get_receiving_multicast_socket(mcast_address, mcast_port)
        recv_socket.settimeout(10)

        ###########################################
        # basic
        try:
            while True:
                data = recv_socket.recv(2048)
                # recv_packets_counter += 1
                to_write_data = data[12:] # dump the 12 byte rtp header
                self.wfile.write(to_write_data) 
        except OSError as err:
            # print (err)
            print ("Viewer left for address:", mcast_address)
            # print (err)
            # STOP the VLC player for the appropriate MULTICAST address
            process_server_request("STOP", mcast_address)
        
        ###########################################
        # try:
        #     # not fully implemented, idea with buffering...
        #     channel_url = mcast_url_map[mcast_address]
        #     response = requests.get(channel_url, stream = True)
        #     data_recv_counter = 0
        #     to_write_data = bytes()
        #     data_buff = []
        #     repeated_data = []
        #     repeat_check = True
        #     for data in tqdm(response.iter_content(chunk_size=1024)):
        #         # if mcast_address in last_written_data_map:
        #         #     if data in last_written_data_map[mcast_address]:
        #         #         print ("Already exists!")

        #         if repeat_check and (mcast_address in last_written_data_map):
        #             written_data = last_written_data_map[mcast_address]

        #             if data in written_data:
        #                 repeated_data.append(data)
        #                 # print ("Already exists!") 
        #                 continue

        #         if repeat_check:
        #             for d in repeated_data[-1:]:
        #                 print ("len =", len(d))
        #                 d_len = len(d)
        #                 target_len = int(d_len/4)
        #                 to_write_data += d[target_len:]
        #             repeated_data = []

        #         repeat_check = False
        #         data_recv_counter += 1
        #         to_write_data += data
        #         data_buff.append(data)
        #         self.wfile.write(data)
        #         if data_recv_counter >=5000:
        #             data_recv_counter = 0
        #             threading.Thread(target=write_to_disk, args = (mcast_address, to_write_data)).start()
        #             to_write_data = bytes()
                    
                    
        # except OSError as err:
        #     # print (err)
        #     print ("Viewer left for address:", mcast_address)
        #     # print (err)
        #     # STOP the VLC player for the appropriate MULTICAST address
        #     process_server_request("STOP", mcast_address)
        ###################################################

def start_file_server(dir, port, mcast_channels_m3u_name):

    if not os.path.exists(os.path.join(dir, mcast_channels_m3u_name)):
        print ("M3U file can NOT be found. Stop it and run it again.")
        return

    host_ip = get_host_name_IP()

    class HTTPServerHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=dir, **kwargs)
        
        def do_POST(self):
            # print (self.headers)

            content_length = int(self.headers['Content-Length'])
            post_body = self.rfile.read(content_length)
  
            # print (post_body)
            
            self.send_response(HTTPStatus.OK)
            self.end_headers()
        

    with http.server.ThreadingHTTPServer(("", port), HTTPServerHandler) as httpd:
        print("Starting HTTP file server for the new M3U file...")
        print("Serving at port", port)
        print ("New m3u url: http://" + host_ip + ":" + str(port) + "/" + mcast_channels_m3u_name)
        httpd.serve_forever()

class ProxyServer(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = False
        self.start()
        self.name = "ProxyServerThread" + str(time.time())

    def run(self):
        # httpd = http.server.ThreadingHTTPServer(listener_addr, StreamHandler, False)
        
        httpd = http.server.HTTPServer(listener_addr, StreamHandler, False)
        httpd.socket = listener_sock
        httpd.server_bind = httpd.server_close = lambda httpd: None
        httpd.serve_forever()

def get_receiving_multicast_socket(multicast_address, multicast_port):

    MULTICAST_GROUP = multicast_address 
    SERVER_ADDRESS = ('', multicast_port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except AttributeError:
        pass
    # sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1) # try with or without
    # sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1) # try with or without

    if 'windows' in platform.system().lower():
        sock.bind(SERVER_ADDRESS)
    else:
        sock.bind((multicast_address, multicast_port))

    group = socket.inet_aton(MULTICAST_GROUP)
    mreq = struct.pack('4sL', group, socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    return sock

# def get_sending_socket():

#     send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#     ttl = struct.pack('b', 1)
#     send_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)

#     return send_socket

def download_m3u_tqdm(url, save_file_name):
    response=requests.get(url, stream = True)
    # req_m3u = requests.get(url)
    # file_name = "channels_original.m3u"
    # print (req_m3u.content.decode()[0])

    total_size = int(response.headers.get('content-length', 0))
    block_size = 1024
    t=tqdm(total=total_size, unit='iB', unit_scale=True)
    with open(save_file_name, "wb") as out_file:
        for data in tqdm(response.iter_content(chunk_size=128)):
            t.update(len(data))
            out_file.write(data)
    t.close()
    if total_size != 0 and t.n != total_size:
        print("ERROR, something went wrong")

def create_multicast_m3u(data_dir, multicast_m3u_file_name, udp_proxy_port = None):

    channel_url_map = {}
    mcast_url_map_NAME = os.path.join(data_dir, "mcast_url_map")

    if os.path.exists(multicast_m3u_file_name):
        if os.path.exists(mcast_url_map_NAME):
            with open(mcast_url_map_NAME, "rb") as channels_map_file:
                channel_url_map = pickle.load(channels_map_file)

            # print (channel_url_map)
            return channel_url_map

    original_m3u_file_name = os.path.join(data_dir, "channels_original.m3u")

    if not os.path.exists(original_m3u_file_name):

        if 'ORIGINAL_M3U_URL' in os.environ:
            original_m3u_url = os.environ['ORIGINAL_M3U_URL']
        else:
            print('No m3u link provided. Exiting...')
            exit()
        
        print ("Downloading original m3u...")
        
        download_m3u_tqdm(original_m3u_url, original_m3u_file_name)
        
      
        print ("Download finished.")
   
    if udp_proxy_port == None:
        print ("Creating new m3u file with multicast addresses...")
    else:
        print ("Creating new m3u file with proxy addresses...")

    original_m3u = open(original_m3u_file_name, "r", encoding="utf-8")

    multicast_m3u = open(multicast_m3u_file_name, "w", encoding="utf-8")
    multicast_m3u.write("#EXTM3U\n")

    lines = original_m3u.readlines()

    first_octet = 239 # static
    second_octet = 123 # static
    third_octet = 1 # from 1 to 254 inclusive
    fourth_octet = 1 # from 1 to 254 inclusive
    print ("Number of channels:", int((len(lines)-1)/2))

    iter_range = range(1, len(lines), 2)

    host_ip = get_host_name_IP()

    for i in tqdm(iter_range):
        curr_line = lines[i]

        if curr_line.startswith("#EXTINF"): # m3u line
            curr_multicast_address = str(first_octet) + "." + str(second_octet) + "." + str(third_octet) + "." + str(fourth_octet)
            # process_m3u_lines((curr_line.strip(), lines[i+1].strip()), curr_multicast_address)

            curr_multicast_m3u_source = ""

            if udp_proxy_port == None:
                curr_multicast_m3u_source = "rtp://" + curr_multicast_address + ":5004"
            else:
                curr_multicast_m3u_source = "http://" + host_ip + ":" + str(udp_proxy_port) + "/rtp/" + curr_multicast_address + ":5004"

            if i != iter_range[-1]:
                curr_multicast_m3u_source += "\n"

            channel_url_map[curr_multicast_address] = lines[i+1].strip()

            multicast_m3u.write(curr_line)
            multicast_m3u.write(curr_multicast_m3u_source)

            if fourth_octet < 254:
                fourth_octet += 1
            else:
                fourth_octet = 1
                third_octet += 1

            if third_octet == 255:
                second_octet += 1
                third_octet = 1
                fourth_octet = 1

    original_m3u.close()
    multicast_m3u.close()
    print ("Finished.")
    print ("Writing channels map to file...")

    with open(mcast_url_map_NAME, "wb") as channels_map_file:
    # pickle.dump(object, file)
        pickle.dump(channel_url_map, channels_map_file)

    print ("Done.")

    return channel_url_map

def create_file_dirs(DATA_DIR, WEB_SERVER_DIRECTORY, mcast_m3u_path, UDP_PROXY_PORT):
    
    
    if os.path.exists(DATA_DIR):
        shutil.rmtree(DATA_DIR)

    if not os.path.exists(DATA_DIR):
        os.makedirs(DATA_DIR)

    if not os.path.exists(WEB_SERVER_DIRECTORY):
        os.makedirs(WEB_SERVER_DIRECTORY)
    
    return create_multicast_m3u(DATA_DIR, mcast_m3u_path, udp_proxy_port=UDP_PROXY_PORT)

####################################################################################

if __name__ == '__main__':
    
    # vlc._default_instance = vlc.Instance(["--file-caching=2000 --network-caching=3000 --live-caching=300 --disc-caching=300 --cr-average=40 --clock-synchro=-1 --clock-jitter=5000"])

    vlc_players_lock = threading.Lock()

    DATA_DIR = "data"



    WEB_SERVER_DIRECTORY = os.path.join(DATA_DIR, "web_server_m3u")
    WEB_SERVER_PORT = 8010



    NUMBER_OF_CLIENTS = 3

    if 'NUMBER_OF_CLIENTS' in os.environ:
        NUMBER_OF_CLIENTS = int(os.environ['NUMBER_OF_CLIENTS'])

    vlc_players_q = queue.Queue()

    for i in range(NUMBER_OF_CLIENTS):
        media_list_player = vlc.MediaListPlayer()
        vlc_players_q.put(media_list_player)


    UDP_PROXY_PORT = 8011

    listener_addr = ('', UDP_PROXY_PORT)
    listener_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener_sock.bind(listener_addr)
    listener_sock.listen(NUMBER_OF_CLIENTS)


    last_written_data_map = {}
    last_written_data_lock = threading.Lock()

    mcast_url_map_lock = threading.Lock()

    vlc_players_dict = {} # key = multicast_address ; value = (vlc_player, number_of_viewers)
    mcast_url_map = {}

    mcast_m3u_path = os.path.join(WEB_SERVER_DIRECTORY, "channels_multicast.m3u")
    # mcast_url_map = create_multicast_m3u(DATA_DIR, mcast_m3u_path, udp_proxy_port=UDP_PROXY_PORT)
    mcast_url_map_lock.acquire()
    mcast_url_map = create_file_dirs(DATA_DIR, WEB_SERVER_DIRECTORY, mcast_m3u_path, UDP_PROXY_PORT)
    mcast_url_map_lock.release()

    http_server_thread = threading.Thread(target = start_file_server, args = (WEB_SERVER_DIRECTORY, WEB_SERVER_PORT, "channels_multicast.m3u", ))
    http_server_thread.name = "HTTPServerThread"
    http_server_thread.start()

    stream_listeners = []

    for i in range(NUMBER_OF_CLIENTS):
        stream_listeners.append(ProxyServer())


    print ("UDP proxy at port " + str(UDP_PROXY_PORT))
    print ("UDP proxy: " + get_host_name_IP() + ":" + str(UDP_PROXY_PORT))

    M3U_UPDATE_TIME = 43200
    # 43200 secs = 12h --> m3u list update from server every 12h

    while True:

        print ("Channels active:", len(vlc_players_dict))
        # for m_addr in vlc_players_dict:
        #     print (mcast_url_map[m_addr])

        data_folder_creation_time = os.path.getctime("data")

        if (time.time() - data_folder_creation_time > M3U_UPDATE_TIME):
            # shutil.rmtree(DATA_DIR)
            mcast_url_map_lock.acquire()
            mcast_url_map = create_file_dirs(DATA_DIR, WEB_SERVER_DIRECTORY, mcast_m3u_path, UDP_PROXY_PORT)
            mcast_url_map_lock.release()
        
        secs = time.time() - data_folder_creation_time
        hours = int(secs/3600)
        mins = int((secs - (hours * 3600)) / 60)
        secs = int(secs - (hours * 3600) - (mins * 60))
        print ("Updated " + str(hours) + "h " + str(mins) + "m " + str(secs) + "s ago.")
        
        time.sleep(600)




# import vlc
# # vlc_instance = vlc.Instance()
# # vlc_instance.media_player_new()
# mlp = vlc.MediaListPlayer()
# mlp = vlc.MediaListPlayer()
# mlp = vlc.MediaListPlayer()

# mp = vlc.MediaPlayer()

# devices = []
# mods = mp.audio_output_device_enum()

# if mods:
#     mod = mods
#     while mod:
#         mod = mod.contents
#         devices.append(mod.device)
#         mod = mod.next
# vlc.libvlc_audio_output_device_list_release(mods)

################


