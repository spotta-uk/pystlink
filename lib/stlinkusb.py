import usb.core
import usb.util
import lib.stlinkex
import re
from typing import Union
from base64 import b32encode
from hashlib import sha1


class StlinkUsbConnector():
    STLINK_CMD_SIZE_V2 = 16

    DEV_TYPES = [
        {
            'version': 'V2',
            'idVendor': 0x0483,
            'idProduct': 0x3748,
            'outPipe': 0x02,
            'inPipe': 0x81,
        }, {
            'version': 'V2-1',
            'idVendor': 0x0483,
            'idProduct': 0x374b,
            'outPipe': 0x01,
            'inPipe': 0x81,
        }, {
            'version': 'V2-1',  # without MASS STORAGE
            'idVendor': 0x0483,
            'idProduct': 0x3752,
            'outPipe': 0x01,
            'inPipe': 0x81,
        }, {
            'version': 'V3E',
            'idVendor': 0x0483,
            'idProduct': 0x374e,
            'outPipe': 0x01,
            'inPipe': 0x81,
        }, {
            'version': 'V3',
            'idVendor': 0x0483,
            'idProduct': 0x374f,
            'outPipe': 0x01,
            'inPipe': 0x81,
        }, {
            'version': 'V3',  # without MASS STORAGE
            'idVendor': 0x0483,
            'idProduct': 0x3753,
            'outPipe': 0x01,
            'inPipe': 0x81,
        }
    ]

    def _generate_device_unique_id(self,vid: int, pid: int, *locations: Union[int, str]) -> str:
        """@brief Generate a semi-stable unique ID from USB device properties.

        This function is intended to be used in cases where a device does not provide a serial number
        string. pyocd still needs a valid unique ID so the device can be selected from amongst multiple
        connected devices. The algorithm used here generates an ID that is stable for a given device as
        long as it is connected to the same USB port.

        @param vid Vendor ID.
        @param pid Product ID.
        @param locations Additional parameters are expected to be int or string values that represent
            parts of the bus location to which the device is connected. At least one location parameter
            must be provided.
        @return Unique ID string generated from parameeters.
        """
        s = f"{vid:4x},{pid:4x}," + ",".join(str(locations))
        return b32encode(sha1(s.encode()).digest()).decode('ascii')
    
    def _get_serial(self):
        # The signature for get_string has changed between versions to 1.0.0b1,
        # 1.0.0b2 and 1.0.0. Try the old signature first, if that fails try
        # the newer one.
        try:
            serial = usb.util.get_string(self._dev, 255, self._dev.iSerialNumber)
        except (usb.core.USBError, ValueError):
            serial = self._generate_device_unique_id(self._dev.idProduct, self._dev.idVendor, self._dev.bus, self._dev.address)
        if serial != None:
            if re.search("[0-9a-fA-f]+", serial).span()[1] != 24:
                serial = ''.join(["%.2x" % ord(c) for c in list(serial)])
        return serial

    def __init__(self, dbg=None, serial = None, index = 0):
        self._dbg = dbg
        self._dev_type = None
        self._xfer_counter = 0
        devices = usb.core.find(find_all=True)
        multiple_devices = False
        self._dev = None
        num_stlink = 0
        for dev in devices:
            for dev_type in StlinkUsbConnector.DEV_TYPES:
                if dev.idVendor == dev_type['idVendor'] and dev.idProduct == dev_type['idProduct']:
                    if not serial and index == 0:
                        if self._dev:
                            multiple_devices = True
                            self._dbg.info("%2d: STLINK %4s, serial %s" % (num_stlink, self._dev_type['version'], self._get_serial()))
                    self._dev = dev
                    self._dev_type = dev_type
                    num_stlink = num_stlink + 1
            if self._dev and serial and serial == self._get_serial():
                break
            if self._dev and serial == None  and index == num_stlink:
                break
        if multiple_devices:
            self._dbg.info("%2d: STLINK %4s, serial %s" %
                           (num_stlink, self._dev_type['version'],
                            self._get_serial()))
            raise lib.stlinkex.StlinkException(
                "Found multiple devices. Select one with -s SERIAL or -n INDEX")
        if self._dev:
            self._dbg.verbose("Connected to ST-Link/%4s, serial %s" % (
                 self._dev_type['version'],  self._get_serial()))
            return
        raise lib.stlinkex.StlinkException('ST-Link/V2 is not connected')

    @property
    def version(self):
        return self._dev_type['version']

    @property
    def xfer_counter(self):
        return self._xfer_counter

    def _write(self, data, tout=200):
        self._dbg.debug("  USB > %s" % ' '.join(['%02x' % i for i in data]))
        self._xfer_counter += 1
        count = self._dev.write(self._dev_type['outPipe'], data, tout)
        if count != len(data):
            raise lib.stlinkex.StlinkException("Error, only %d Bytes was transmitted to ST-Link instead of expected %d" % (count, len(data)))

    def _read(self, size, tout=200):
        read_size = size
        if read_size < 64:
            read_size = 64
        elif read_size % 4:
            read_size += 3
            read_size &= 0xffc
        data = self._dev.read(self._dev_type['inPipe'], read_size, tout).tolist()
        self._dbg.debug("  USB < %s" % ' '.join(['%02x' % i for i in data]))
        return data[:size]

    def xfer(self, cmd, data=None, rx_len=None, retry=0, tout=200):
        while (True):
            try:
                if len(cmd) > self.STLINK_CMD_SIZE_V2:
                    raise lib.stlinkex.StlinkException("Error too many Bytes in command: %d, maximum is %d" % (len(cmd), self.STLINK_CMD_SIZE_V2))
                # pad to 16 bytes
                cmd += [0] * (self.STLINK_CMD_SIZE_V2 - len(cmd))
                self._write(cmd, tout)
                if data:
                    self._write(data, tout)
                if rx_len:
                    return self._read(rx_len)
            except usb.core.USBError as e:
                if retry:
                    retry -= 1
                    continue
                raise lib.stlinkex.StlinkException("USB Error: %s" % e)
            return None

    def unmount_discovery(self):
        import platform
        if platform.system() != 'Darwin' or self.version != 'V2-1':
            return
        import subprocess
        p = subprocess.Popen(
            ['diskutil', 'info', 'DISCOVERY'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        p.wait()
        out, err = p.communicate()
        out = out.decode(encoding='UTF-8').strip()
        is_mounted = False
        is_mbed = False
        for line in out.splitlines():
            param = line.split(':', 1)
            if param[0].strip() == 'Mounted' and param[1].strip() == 'Yes':
                is_mounted = True
            if param[0].strip() == 'Device / Media Name' and param[1].strip().startswith('MBED'):
                is_mbed = True
        if is_mounted and is_mbed:
            print("unmounting DISCOVERY")
            p = subprocess.Popen(
                ['diskutil', 'unmount', 'DISCOVERY'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            p.wait()
