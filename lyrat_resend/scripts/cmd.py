#!/usr/bin/env python3
"""
Send commands to ESP32 LyraT via UDP.
ESP32 listens on port 5006 for control commands.

Usage:
  python cmd.py status
  python cmd.py hpf 1
  python cmd.py mg 4
  python cmd.py sns 2
  python cmd.py snsg 20

Interactive mode (no args):
  python cmd.py
"""

import sys
import socket

ESP32_IP   = "192.168.1.183"
CMD_PORT   = 5006

def send_cmd(cmd):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(cmd.encode('ascii') + b'\n', (ESP32_IP, CMD_PORT))
    sock.close()
    print(f"  -> {ESP32_IP}:{CMD_PORT}  '{cmd}'")

def main():
    if len(sys.argv) > 1:
        cmd = ' '.join(sys.argv[1:])
        send_cmd(cmd)
    else:
        print(f"ESP32 command sender ({ESP32_IP}:{CMD_PORT})")
        print("Type commands, Ctrl+C to quit.\n")
        print("Pipeline: hw N | hpf | mg N | notch | w | g | aec | sns 0/1/2 | snsg N")
        print("Other:    lim | cg N | vol N | wifi | all | s | h\n")
        try:
            while True:
                cmd = input("> ").strip()
                if cmd:
                    send_cmd(cmd)
        except (KeyboardInterrupt, EOFError):
            print()

if __name__ == "__main__":
    main()
