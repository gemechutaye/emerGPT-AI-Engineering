// Lightweight paper physics shared by both pages.
const colors=['#e5b7c7','#92c7ac','#e8c862','#b9addb','#90bad2','#f5eee0'];
export function createConfetti(host){
const lifetime=new AbortController();
let canvas,ctx,backCanvas,backCtx,ball,particles=[],frame=0,last=0,width=0,height=0;
function resize(){
 width=innerWidth;height=innerHeight;
 const ratio=Math.min(devicePixelRatio||1,2);
 for(const [surface,context] of [[canvas,ctx],[backCanvas,backCtx]]){surface.width=Math.round(width*ratio);surface.height=Math.round(height*ratio);context.setTransform(ratio,0,0,ratio,0,0);}
}
function setup(){
 if(canvas)return;
 ball=host.querySelector('img');
 canvas=document.createElement('canvas');backCanvas=document.createElement('canvas');canvas.className='party-confetti';backCanvas.className='party-confetti party-confetti-back';for(const c of [canvas,backCanvas])c.setAttribute('aria-hidden','true');document.body.append(backCanvas,canvas);ctx=canvas.getContext('2d');backCtx=backCanvas.getContext('2d');
 if(!ctx||!backCtx){canvas.remove();backCanvas.remove();canvas=null;ctx=null;backCtx=null;return;}
 resize();window.addEventListener('resize',resize,{passive:true,signal:lifetime.signal});
 document.addEventListener('visibilitychange',()=>{if(document.hidden)clear();},{signal:lifetime.signal});
 window.addEventListener('pagehide',clear,{signal:lifetime.signal});
}
function clear(){cancelAnimationFrame(frame);frame=0;particles=[];ctx?.clearRect(0,0,width,height);backCtx?.clearRect(0,0,width,height);}
function draw(time){
 const dt=Math.min((time-last)/1000,.033);last=time;ctx.clearRect(0,0,width,height);backCtx.clearRect(0,0,width,height);
 particles=particles.filter(p=>p.age<7.5&&p.y<height+45);
 for(const p of particles){
  p.age+=dt;p.vx*=Math.pow(.39,dt);p.vy+=330*dt;p.vy*=Math.pow(.82,dt);
  p.x+=(p.vx+Math.sin(p.age*4+p.phase)*23)*dt;p.y+=p.vy*dt;
  p.angle+=p.spin*dt;p.flip+=p.tumble*dt;
  const face=Math.cos(p.flip),light=.73+.27*Math.abs(face),fade=Math.min(1,(7.5-p.age)*2);
  const c=p.front?ctx:backCtx;
  c.save();c.translate(p.x,p.y);c.rotate(p.angle);c.scale(1,Math.max(.09,Math.abs(face)));
  c.globalAlpha=Math.max(0,fade);c.fillStyle=p.color;
  c.shadowColor='rgba(72,47,22,.13)';c.shadowBlur=2;c.shadowOffsetX=1;c.shadowOffsetY=2;
  c.fillRect(-p.size/2,-p.size*p.shape/2,p.size,p.size*p.shape);
  c.shadowColor='transparent';c.globalAlpha=Math.max(0,fade*(1-light));
  c.fillStyle=face<0?'#fff':'#453e39';c.fillRect(-p.size/2,-p.size*p.shape/2,p.size,p.size*p.shape);
  c.restore();
 }
 // The foreground paper is also occluded by the approved ball's real silhouette.
 // The image's opacity in CSS does not affect its alpha when used as this mask.
 if(ball?.complete&&ball.naturalWidth){const r=ball.getBoundingClientRect();if(r.bottom>0&&r.top<height){ctx.save();ctx.globalCompositeOperation='destination-out';ctx.drawImage(ball,r.left,r.top,r.width,r.height);ctx.restore();}}
 if(particles.length)frame=requestAnimationFrame(draw);else frame=0;
}
function burst({small=false,count:requested,nearBall=false}={}){
 if(matchMedia('(prefers-reduced-motion: reduce)').matches)return;setup();if(!ctx)return;
 const count=Number.isFinite(requested)?Math.min(180,Math.max(8,requested)):small?60:Math.min(190,Math.round(width/6)+70);
 const rect=nearBall&&ball?ball.getBoundingClientRect():null;
 const origin=rect&&rect.bottom>0&&rect.top<height?Math.min(height*.62,Math.max(100,rect.top+rect.height*.62)):Math.min(height*.55,440);
 for(let i=0;i<count;i++){
  const left=i%2===0,speed=(340+Math.random()*360)*(small?.8:1);
  const angle=left?-.85-Math.random()*.5:-Math.PI+.85+Math.random()*.5;
  particles.push({front:Math.random()<.08,x:left?-8:width+8,y:origin+(Math.random()-.5)*32,vx:Math.cos(angle)*speed,vy:Math.sin(angle)*speed,age:0,phase:Math.random()*6.28,angle:Math.random()*6.28,flip:Math.random()*6.28,spin:(Math.random()-.5)*12,tumble:6+Math.random()*10,size:4+Math.random()*5,shape:1+Math.random()*1.3,color:colors[i%colors.length]});
 }
 particles=particles.slice(-360);
 if(!frame){last=performance.now();frame=requestAnimationFrame(draw);}
}

return {burst,destroy(){lifetime.abort();clear();canvas?.remove();backCanvas?.remove();}};
}
