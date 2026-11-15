"""Run one size per fresh process: python -m scripts.benchmark 8192 4096."""
import json
import resource
import sys
import tempfile
import time
from pathlib import Path
from aurelia_utils.engine import Params,geography,render_field

p=Params(width=int(sys.argv[1]),height=int(sys.argv[2]),seed='Aurelia benchmark')
start=time.perf_counter()
with tempfile.TemporaryDirectory() as d:
    field=geography(p)
    share=render_field(field,p.width,p.height,Path(d)/'map.png')
    print(json.dumps({'size':[p.width,p.height],'seconds':round(time.perf_counter()-start,2),
                      'peak_rss_mib':round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,1),
                      'png_bytes':(Path(d)/'map.png').stat().st_size,'land_pixel_share':round(share,5)}))
