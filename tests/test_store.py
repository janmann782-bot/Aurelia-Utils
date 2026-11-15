import pytest
from aurelia_utils.config import Config
from aurelia_utils.store import Store,Busy
from aurelia_utils.worker import run_job,job_dir
from aurelia_utils.engine import Params
from dataclasses import asdict


def test_sessions_ownership_recovery(tmp_path):
    s=Store(tmp_path)
    s.set_session(1,{'await':'seed'})
    assert Store(tmp_path).session(1)=={'await':'seed'}
    assert s.animation(1) and not s.animation(1,True)
    jid=s.enqueue(1,'seed',{'params':asdict(Params(width=128,height=64))})
    assert s.get(jid,2) is None
    with pytest.raises(Busy): s.enqueue(1,'seed',{})
    assert s.claim()['id']==jid
    s.recover()
    assert s.get(jid)['status']=='queued'
    assert s.claim()['id']==jid
    run_job(Config(tmp_path),s,s.get(jid))
    assert s.get(jid)['status']=='ready'
    assert (job_dir(Config(tmp_path),1,jid)/'project.zip').exists()


def test_cancellation_ack_and_source_atomic(tmp_path):
    s=Store(tmp_path)
    src=tmp_path/'upload'
    src.write_bytes(b'original')
    jid=s.enqueue(1,'inspect',{},source=src)
    assert (tmp_path/'users'/'1'/jid/'source').read_bytes()==b'original'
    s.claim()
    s.cancel(jid,1)
    assert s.get(jid)['status']=='cancelling'
    assert not s.remove(jid,1)
    with pytest.raises(InterruptedError): s.progress(jid,'next')
    s.acknowledge_cancel(jid)
    assert s.remove(jid,1)
    with pytest.raises(ValueError): job_dir(Config(tmp_path),1,'../../escape')


def test_double_cancel_does_not_allow_early_removal(tmp_path):
    s=Store(tmp_path)
    jid=s.enqueue(1,'seed',{})
    s.claim()
    s.cancel(jid,1)
    s.cancel(jid,1)
    assert s.get(jid)['status']=='cancelling'
    assert not s.remove(jid,1)
    s.recover()
    assert s.get(jid)['status']=='cancelled'


def test_sketch_pipeline_retains_original(tmp_path):
    import hashlib,zipfile,json
    from PIL import Image,ImageDraw
    source=tmp_path/'input.png'
    im=Image.new('L',(256,128),255)
    ImageDraw.Draw(im).ellipse((50,30,200,100),fill=0)
    im.save(source)
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    s=Store(tmp_path)
    cfg=Config(tmp_path)
    parent=s.enqueue(1,'inspect',{},source=source)
    run_job(cfg,s,s.claim())
    assert s.get(parent)['status']=='ready'
    child=s.enqueue(1,'sketch',{'params':asdict(Params(width=256,height=128)),
                              'source_job':parent,'polarity':'dark','mode':'strict'})
    run_job(cfg,s,s.claim())
    assert s.get(child)['status']=='ready'
    with zipfile.ZipFile(job_dir(cfg,1,child)/'project.zip') as z:
        assert hashlib.sha256(z.read('original/source')).hexdigest()==before
        assert json.loads(z.read('metadata.json'))['kind']=='sketch'
