#!/usr/bin/env python

import pygtk
pygtk.require('2.0')
import gtk
import gobject
import sys
import logging
from xpra.platform import init
from xpra.clipboard.gdk_clipboard import GDKClipboardProtocolHelper

SELECTION="CLIPBOARD"

class FakeRemoteClipboard(object):

	def __init__(self):
		CLIPBOARDS = [SELECTION]
		self.helper = GDKClipboardProtocolHelper(self.send_packet_cb, self.progress_cb, clipboards=CLIPBOARDS)

	def send_packet_cb(self, *packet):
		print("send_packet_cb(%s)" % str(packet))
		if packet[0]=="clipboard-request":
			num = packet[1]
			assert packet[2]==SELECTION
			if packet[3]=="TARGETS":
				gobject.timeout_add(100, self.fake_target, num)
			elif packet[3]=="UTF8_STRING":
				gobject.timeout_add(100, self.fake_data, num)
	def progress_cb(self, *args):
		print("progress_cb(%s)" % str(args))
	def fake_packet(self, packet):
		print("fake_packet(%s)" % str(packet))
		self.helper.process_clipboard_packet(packet)
	def fake_token(self, *args):
		self.fake_packet(("clipboard-token", SELECTION))
	def fake_target(self, num, *args):
		self.fake_packet(("clipboard-contents", num, SELECTION, "ATOM", 32, "atoms", ("UTF8_STRING",)))
	def fake_data(self, num, *args):
		self.fake_packet(("clipboard-contents", num, SELECTION, "UTF8_STRING", 8, "bytes", "hello"))


def main():
	logging.basicConfig(format="%(asctime)s %(message)s")
	logging.root.setLevel(logging.DEBUG)

	from xpra.os_util import set_application_name, set_prgname
	set_prgname("Primary Clipboard Test Tool")
	set_application_name("Primary Clipboard Test Tool")
	if sys.platform.startswith("win"):
		from xpra.platform.win32 import set_redirect_output, set_log_filename
		set_redirect_output(True)
		set_log_filename("Primary_Clipboard_Test.log")
	init()

	frc = FakeRemoteClipboard()
	gobject.timeout_add(1000, frc.fake_token)
	#gobject.timeout_add(1200, fake_target, 0)
	#gobject.timeout_add(1400, fake_target, 1)
	#gobject.timeout_add(1600, fake_target, 2)
	#gobject.timeout_add(1800, fake_data, 2)
	#gobject.timeout_add(2500, fake_data, 3)
	#gobject.timeout_add(3500, fake_data, 5)
	gtk.main()


if __name__ == "__main__":
	main()
