"""python -m scripts.demo: reproducible procedural examples, no image generation API."""
from pathlib import Path
from PIL import Image, ImageDraw
from aurelia_utils.engine import Params, geography, render_field, render_sketch


def main():
    out=Path('examples')
    out.mkdir(exist_ok=True)
    p=Params(seed='Aurelia Utils 2057')
    field=geography(p)
    render_field(field,p.width,p.height,out/'seed_map.png')
    render_field(field,1280,640,out/'seed_preview.png')
    source=Image.new('RGB',(1024,512),'white')
    draw=ImageDraw.Draw(source)
    draw.polygon([(110,90),(270,75),(340,145),(290,235),(360,300),(260,420),
                  (145,350),(170,245),(80,200)],fill='black')
    draw.polygon([(550,110),(670,60),(860,130),(925,250),(820,375),(680,420),
                  (590,300),(620,220)],fill='black')
    draw.ellipse((440,180,470,225),fill='black')
    draw.point((390,370),fill='black')
    source.save(out/'sketch_input.png')
    p=Params(seed='Aurelia Utils 2057',width=1024,height=512)
    render_sketch(out/'sketch_input.png',p,out/'sketch_strict.png')
    render_sketch(out/'sketch_input.png',p,out/'sketch_coast.png',mode='coast')


if __name__=='__main__': main()
