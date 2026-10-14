import sys, os, re, io

path = sys.argv[1]
out  = sys.argv[2]
maxread = int(sys.argv[3]) if len(sys.argv) > 3 else 0
minlen = 4
mx = 80
pat = re.compile(rb'[A-Za-z0-9_\-\. /:]{4,80}')
found = set()
read = 0
CH = 8 * 1024 * 1024
overlap = 256
tail = b''
with open(path, 'rb') as f:
    while True:
        buf = f.read(CH)
        if not buf:
            break
        read += len(buf)
        data = tail + buf
        for m in pat.finditer(data):
            s = m.group()
            if minlen <= len(s) <= mx:
                # 只保留看起来像标识符/名字的
                t = s.decode('latin-1').strip()
                if t and ((' ' in t) or ('/' in t) or ('_' in t) or t.isalnum() or '.' in t):
                    found.add(t)
        tail = data[-overlap:]
        if maxread and read >= maxread:
            break
found = sorted(found)
with open(out, 'w', encoding='utf-8', errors='replace') as g:
    g.write('\n'.join(found))
print('file=%s read=%d unique=%d -> %s' % (path, read, len(found), out))
