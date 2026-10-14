"""在 exe 里直接定位 FName 条目，打印真实字节，判定头宽（2 字节还是 4 字节）。"""
import struct, sys, os

path = sys.argv[1] if len(sys.argv) > 1 else \
    r"E:\steam\steamapps\common\MECCHA CHAMELEON\Chameleon\Binaries\Win64\PenguinHotel-Win64-Shipping.exe"
data = open(path, "rb").read()
print("size=%.1fMB" % (len(data) / 1048576.0))

def show(tag, needle, window=48, limit=6):
    print("\n== %s (%r) ==" % (tag, needle))
    start = 0
    hits = []
    while len(hits) < limit:
        i = data.find(needle, start)
        if i < 0:
            break
        hits.append(i)
        start = i + 1
    for i in hits:
        pre = data[max(0, i - window):i]
        print("  off=0x%08X" % i)
        print("    pre : %s" % pre.hex(" "))
        print("    post: %s" % data[i:i + len(needle) + 24].hex(" "))
        # 解释为 2 字节头
        h = struct.unpack_from("<H", data, i - 2)[0]
        print("    2B头@-2: 0x%04X -> len=%d wide=%d" % (h, (h >> 1) & 0x7FFF, h & 1))
        h4 = struct.unpack_from("<I", data, i - 4)[0]
        print("    4B头@-4: 0x%08X -> len=%d wide=%d" % (h4, (h4 >> 1) & 0x7FFFFFFF, h4 & 1))
    if not hits:
        print("  <未找到>")

show("ByteProperty", b"ByteProperty")
show("IntProperty", b"IntProperty")
show("FloatProperty", b"FloatProperty")
show("None\\0", b"None\x00", window=24, limit=4)
show("UTF16 ByteProperty", "ByteProperty".encode("utf-16-le"), window=32, limit=4)
show("PenguinHotel", b"PenguinHotel", window=16, limit=3)
show("BP_FirstPersonCharacter", b"BP_FirstPersonCharacter", window=16, limit=3)
show("PlayerCameraManager", b"PlayerCameraManager", window=16, limit=3)
show("HiderBeacon", b"Hider", window=16, limit=4)
