import math
import struct
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

class DataTypes:
    def parsebool(refobj,payload):
        if payload == 'True' or payload == 'true' or payload == '1' or payload == 'TRUE':
            value = True
        elif payload == 'False' or payload == 'false' or payload == '0' or payload == 'FALSE':
            value = False
        else:
            value = None
        return value

    def combinebool(refobj,val):
        try:
            len(val)
            return bool(val[0])
        except Exception:
            return bool(val)

    def parseString(refobj,msg):
        out=[]
        if len(msg)<=refobj.stringLength:
            for x in range(1,len(msg)+1):
                if math.fmod(x,2)>0:
                    out.append(ord(msg[x-1])<<8)
                else:
                    pass
                    out[int(x/2-1)]+=ord(msg[x-1])
        else:
            out = None
        return out
    def combineString(refobj,val):
        out=""
        for x in val:
            out+=chr(x>>8)
            out+=chr(x&0x00FF)
        return out

    def parseint16(refobj,msg):
        try:
            value=int(msg)
            if value > 32767 or value < -32768:
                out = None
            else:
                out = value&0xFFFF
        except Exception:
            out=None
        return out
    def combineint16(refobj,val):
        try:
            len(val)
            myval=val[0]
        except Exception:
            myval=val

        if (myval & 0x8000) > 0:
            out = -((~myval & 0x7FFF)+1)
        else:
            out = myval
        return out

    def parseuint32LE(refobj,msg):
        try:
            value=int(msg)
            if value > 4294967295 or value < 0:
                out = None
            else:
                out=[int(value>>16),int(value&0x0000FFFF)]
        except Exception:
            out=None
        return out
    def combineuint32LE(refobj,val):
        out = val[0]*65536 + val[1]
        return out

    def parseuint32BE(refobj,msg):
        try:
            value=int(msg)
            if value > 4294967295 or value < 0:
                out = None
            else:
                out=[int(value&0x0000FFFF),int(value>>16)]
        except Exception:
            out=None
        return out
    def combineuint32BE(refobj,val):
        out = val[0] + val[1]*65536
        return out

    def parseint32LE(refobj,msg):
        try:
            value = int(msg)
            if value > 2147483647 or value < -2147483648:
                return None
            value_unsigned = value & 0xFFFFFFFF
            return [int(value_unsigned >> 16), int(value_unsigned & 0x0000FFFF)]
        except Exception:
            return None
    def combineint32LE(refobj,val):
        out = val[0]*65536 + val[1]
        out = int.from_bytes(out.to_bytes(4, 'little', signed=False), 'little', signed=True)
        return out

    def parseint32BE(refobj,msg):
        try:
            value = int(msg)
            if value > 2147483647 or value < -2147483648:
                return None
            value_unsigned = value & 0xFFFFFFFF
            return [int(value_unsigned & 0x0000FFFF), int(value_unsigned >> 16)]
        except Exception:
            return None
    def combineint32BE(refobj,val):
        out = val[0] + val[1]*65536
        out = int.from_bytes(out.to_bytes(4, 'big', signed=False), 'big', signed=True)
        return out

    def parseuint16(refobj,msg):
        try:
            value=int(msg)
            if value > 65535 or value < 0:
                value = None
        except Exception:
            value=None
        return value
    def combineuint16(refobj,val):
        try:
            len(val)
            return val[0]
        except Exception:
            return val

    def parsefloat32LE(refobj,msg):
        try:
            packed = struct.unpack('=I', struct.pack('=f', float(msg)))[0]
            return [int(packed >> 16), int(packed & 0x0000FFFF)]
        except Exception:
            return None
    def combinefloat32LE(refobj,val):
        out = str(struct.unpack('=f', struct.pack('=I',int(val[0])<<16|int(val[1])))[0])
        return out

    def parsefloat32BE(refobj,msg):
        try:
            packed = struct.unpack('=I', struct.pack('=f', float(msg)))[0]
            return [int(packed & 0x0000FFFF), int(packed >> 16)]
        except Exception:
            return None
    def combinefloat32BE(refobj,val):
        out = str(struct.unpack('=f', struct.pack('=I',int(val[1])<<16|int(val[0])))[0])
        return out

    def parseListUint16(refobj,msg):
        out=[]
        try:
            msg=msg.rstrip()
            msg=msg.lstrip()
            msg=msg.split(" ")
            if len(msg) != refobj.regAmount:
                return None
            for x in range(0, len(msg)):
                out.append(int(msg[x]))
        except Exception:
            return None
        return out
    def combineListUint16(refobj,val):
        out=""
        for x in val:
            out+=str(x)+" "
        return out

    def _refTargetZone(refobj):
        # Resolve an optional IANA timezone configured on the reference's HA
        # JSON (e.g. {"timezone": "America/Los_Angeles"}). Returns a ZoneInfo
        # or None. Unknown zone names are treated as not configured.
        j = getattr(refobj, 'json', None) or {}
        name = j.get('timezone')
        if not name:
            return None
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            return None

    def parsePackedTime(refobj,msg):
        # Accepts ISO 8601 "YYYY-MM-DDTHH:MM:SS[.fff][±HH:MM|Z]" and the
        # legacy "YYYY-MM-DD HH:MM:SS" form. The device clock is wall-clock
        # local; an aware input is converted to the configured zone (or
        # the host's local zone) before stripping tzinfo.
        s = msg.strip().replace('Z', '+00:00').replace(' ', 'T')
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is not None:
            target = DataTypes._refTargetZone(refobj)
            dt = dt.astimezone(target) if target else dt.astimezone()
            dt = dt.replace(tzinfo=None)
        reg0 = ((dt.year % 100) << 8) | dt.month
        reg1 = (dt.day << 8) | dt.hour
        reg2 = (dt.minute << 8) | dt.second
        return [reg0, reg1, reg2]
    def combinePackedTime(refobj,val):
        year   = (val[0] >> 8) & 0xFF
        month  =  val[0] & 0xFF
        day    = (val[1] >> 8) & 0xFF
        hour   =  val[1] & 0xFF
        minute = (val[2] >> 8) & 0xFF
        second =  val[2] & 0xFF
        try:
            dt = datetime(2000 + year, month, day, hour, minute, second)
        except ValueError:
            return None
        if DataTypes._refTargetZone(refobj) is not None:
            # Naive ISO; HA's discovery `timezone` field interprets it.
            return dt.isoformat(timespec='seconds')
        # No tz configured — attach the host's local offset so HA gets
        # a fully-qualified timestamp.
        return dt.astimezone().isoformat(timespec='seconds')

    def parsehiByte(refobj,msg):
        try:
            value=int(msg)
            if value > 255 or value < 0:
                return None
            return value << 8
        except Exception:
            return None
    def combinehiByte(refobj,val):
        try:
            return (val[0] >> 8) & 0xFF
        except Exception:
            return (val >> 8) & 0xFF

    def parseloByte(refobj,msg):
        try:
            value=int(msg)
            if value > 255 or value < 0:
                return None
            return value & 0xFF
        except Exception:
            return None
    def combineloByte(refobj,val):
        try:
            return val[0] & 0xFF
        except Exception:
            return val & 0xFF

    def parseDataType(refobj,conf):
        if conf is None or conf == "uint16" or conf == "":
            refobj.regAmount=1
            refobj.parse=DataTypes.parseuint16
            refobj.combine=DataTypes.combineuint16
        elif conf.startswith("list-uint16-"):
            try:
                length = int(conf[12:15])
            except Exception:
                length = 1
            if length > 50:
                print("Data type list-uint16: length too long")
                length = 50
            refobj.parse=DataTypes.parseListUint16
            refobj.combine=DataTypes.combineListUint16
            refobj.regAmount=length
        elif conf.startswith("string"):
            try:
                length = int(conf[6:9])
            except Exception:
                length = 2
            if length > 100:
                print("Data type string: length too long")
                length = 100
            if  math.fmod(length,2) != 0:
                length=length-1
                print("Data type string: length must be divisible by 2")
            refobj.parse=DataTypes.parseString
            refobj.combine=DataTypes.combineString
            refobj.stringLength=length
            refobj.regAmount=int(length/2)
        elif conf == "int32LE":
            refobj.parse=DataTypes.parseint32LE
            refobj.combine=DataTypes.combineint32LE
            refobj.regAmount=2
        elif conf == "int32BE":
            refobj.regAmount=2
            refobj.parse=DataTypes.parseint32BE
            refobj.combine=DataTypes.combineint32BE
        elif conf == "int16":
            refobj.regAmount=1
            refobj.parse=DataTypes.parseint16
            refobj.combine=DataTypes.combineint16
        elif conf == "uint32LE":
            refobj.regAmount=2
            refobj.parse=DataTypes.parseuint32LE
            refobj.combine=DataTypes.combineuint32LE
        elif conf == "uint32BE":
            refobj.regAmount=2
            refobj.parse=DataTypes.parseuint32BE
            refobj.combine=DataTypes.combineuint32BE
        elif conf == "bool":
            refobj.regAmount=1
            refobj.parse=DataTypes.parsebool
            refobj.combine=DataTypes.combinebool
        elif conf == "float32LE":
            refobj.regAmount=2
            refobj.parse=DataTypes.parsefloat32LE
            refobj.combine=DataTypes.combinefloat32LE
        elif conf == "float32BE":
           refobj.regAmount=2
           refobj.parse=DataTypes.parsefloat32BE
           refobj.combine=DataTypes.combinefloat32BE
        elif conf == "packedtime":
            refobj.regAmount=3
            refobj.parse=DataTypes.parsePackedTime
            refobj.combine=DataTypes.combinePackedTime
        elif conf == "hibyte":
            refobj.regAmount=1
            refobj.parse=DataTypes.parsehiByte
            refobj.combine=DataTypes.combinehiByte
        elif conf == "lobyte":
            refobj.regAmount=1
            refobj.parse=DataTypes.parseloByte
            refobj.combine=DataTypes.combineloByte
