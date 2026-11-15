import hashlib
from dataclasses import replace
import numpy as np
import pytest
from PIL import Image
from aurelia_utils.engine import (Params,SEA,LAND,geography,render_field,
                                 render_sketch,sketch_mask,inspect_image)


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def test_seed_reproducible_and_different(tmp_path):
    p=Params(seed='земля',width=512,height=256)
    for name,seed in [('a','земля'),('b','земля'),('c','другой')]:
        field=geography(replace(p,seed=seed))
        render_field(field,512,256,tmp_path/f'{name}.png')
    assert digest(tmp_path/'a.png')==digest(tmp_path/'b.png')
    assert digest(tmp_path/'a.png')!=digest(tmp_path/'c.png')
    colors=np.unique(np.asarray(Image.open(tmp_path/'a.png')).reshape(-1,3),axis=0)
    assert set(map(tuple,colors))=={LAND,SEA}


def test_land_fraction_and_seam():
    p=Params(width=1024,height=512,land=40)
    field=geography(p)
    assert abs((field>=0).mean()-.4)<.001
    seam=np.abs(field[:,0]-field[:,-1]).mean()
    assert seam<np.abs(np.diff(field,axis=1)).mean()*5


def test_exact_import_tiny_island_and_lake(tmp_path):
    a=np.full((64,128,3),255,np.uint8)
    a[12:40,20:70]=0
    a[20,30]=255
    a[50,100]=0
    source=tmp_path/'source.png'
    Image.fromarray(a).save(source)
    before=digest(source)
    p=Params(width=128,height=64)
    render_sketch(source,p,tmp_path/'map.png')
    mask=sketch_mask(source)
    actual=np.all(np.asarray(Image.open(tmp_path/'map.png'))==LAND,axis=2)
    assert np.array_equal(mask,actual)
    assert actual[50,100] and not actual[20,30]
    assert digest(source)==before
    render_sketch(source,replace(p,width=256,height=128),tmp_path/'big.png')
    big=np.all(np.asarray(Image.open(tmp_path/'big.png'))==LAND,axis=2)
    assert np.array_equal(big,np.repeat(np.repeat(mask,2,axis=0),2,axis=1))


def test_alpha_and_polarity(tmp_path):
    a=np.full((64,128,4),255,np.uint8)
    a[0:10,:,3]=0
    a[20:30,20:30,:3]=0
    Image.fromarray(a).save(tmp_path/'s.png')
    light=sketch_mask(tmp_path/'s.png','light')
    dark=sketch_mask(tmp_path/'s.png','dark')
    assert not light[:10].any() and not dark[:10].any()
    assert dark[20:30,20:30].all()
    assert not (light & dark).any()


def test_invalid_files_and_resize(tmp_path):
    path=tmp_path/'not.png'
    path.write_text('<script>not an image</script>')
    with pytest.raises(ValueError): inspect_image(path)
    im=Image.new('L',(128,64),255)
    im.putpixel((40,30),0)
    im.save(path)
    with pytest.raises(ValueError): render_sketch(path,Params(width=64,height=32),tmp_path/'bad.png')
    with pytest.raises(ValueError): render_sketch(path,Params(width=256,height=64),tmp_path/'bad.png')
    with pytest.raises(ValueError): Params(width=16384,height=16384).validate()


def test_coast_opt_in_and_deterministic(tmp_path):
    a=np.full((256,512),255,np.uint8)
    a[70:190,100:350]=0
    Image.fromarray(a).save(tmp_path/'s.png')
    p=Params(width=512,height=256)
    render_sketch(tmp_path/'s.png',p,tmp_path/'strict.png')
    for n in ('a','b'):
        render_sketch(tmp_path/'s.png',p,tmp_path/f'{n}.png',mode='coast')
    assert digest(tmp_path/'a.png')==digest(tmp_path/'b.png')
    assert digest(tmp_path/'a.png')!=digest(tmp_path/'strict.png')


@pytest.mark.large
def test_8k_stream_and_cancel_atomic(tmp_path):
    p=Params(width=8192,height=4096)
    field=geography(p)
    calls=[]
    share=render_field(field,p.width,p.height,tmp_path/'8k.png',lambda s,d,t:calls.append((s,d,t)))
    with Image.open(tmp_path/'8k.png') as im:
        assert im.size==(8192,4096)
        im.verify()
    assert abs(share-.35)<.01
    assert calls[-1][1:]==(4096,4096)
    def cancel(*_): raise InterruptedError()
    with pytest.raises(InterruptedError): render_field(field,8192,4096,tmp_path/'cancel.png',cancel)
    assert not (tmp_path/'cancel.png').exists()
    assert not (tmp_path/'cancel.png.part').exists()


@pytest.mark.large
def test_8k_sketch_pixel_preservation(tmp_path):
    source=tmp_path/'source.png'
    im=Image.new('L',(8192,4096),255)
    from PIL import ImageDraw
    draw=ImageDraw.Draw(im)
    draw.rectangle((500,500,5000,3000),fill=0)
    draw.point((8000,4000),fill=0)
    draw.point((1000,1000),fill=255)
    im.save(source)
    del im
    render_sketch(source,Params(width=8192,height=4096),tmp_path/'out.png')
    with Image.open(tmp_path/'out.png') as result:
        assert result.size==(8192,4096)
        assert result.getpixel((8000,4000))==LAND
        assert result.getpixel((1000,1000))==SEA
