// Adapted from Birthday Traffic; see PROVENANCE.md.
import {DiscoMotor,phaseOf,TAU,clamp} from './disco-motion.mjs';
import {createDiscoRenderer} from './disco-renderer.mjs';

export function initDisco(host,{onGrab=()=>{},onBrush=()=>{},onRelease=()=>{},onSpin=()=>{}}={}){
 const lifetime=new AbortController();let disposed=false,observer=null;
 const listen=(target,event,handler,options={})=>target.addEventListener(event,handler,{...(typeof options==='boolean'?{capture:options}:options),signal:lifetime.signal});
 const poster=host?.querySelector('img');if(!poster)return{boost(){},destroy(){}};
 const still=poster.src,motion=matchMedia('(prefers-reduced-motion: reduce)'),connection=navigator.connection;
 let motor=new DiscoMotor(7.2),renderer=null,media=null,videos=[],active=null,lastVideo=null,lastTime=-1,lastRAF=0,raf=0;
 let visible=true,loading=false,failed=false,dragging=null,hover=null,suppressClick=false,playing=null,forceSync=true,framesShown=false,playBlocked=false;
 const allowed=()=>!disposed&&visible&&!document.hidden&&!motion.matches&&!connection?.saveData&&!failed;
 const pauseVideos=()=>{for(const v of videos)v.pause();playing=null;};
 const announce=text=>{const note=host.parentElement?.querySelector('[role=status]');if(note)note.textContent=text;};
 function fallback(){
  if(disposed)return;
  failed=true;cancelAnimationFrame(raf);raf=0;pauseVideos();renderer?.destroy();renderer=null;
  host.classList.remove('disco-controlled','is-grabbing','disco-interactive');
  host.style.touchAction='';poster.src=allowedFallback()?media?.animation||still:still;
  host.setAttribute('aria-label','Tap the disco ball for confetti');
 }
 function allowedFallback(){return !disposed&&visible&&!document.hidden&&!motion.matches&&!connection?.saveData;}
 function sync(){
  if(disposed)return;
  if(failed){poster.src=allowedFallback()?media?.animation||still:still;return;}
  if(!allowed()){
   cancelAnimationFrame(raf);raf=0;lastRAF=0;pauseVideos();motor?.reset();dragging=null;host.classList.remove('is-grabbing');
   if(motion.matches||connection?.saveData){host.classList.remove('disco-controlled');poster.src=still;}
   return;
  }
  if(media&&!loading)load();
  if(renderer&&!raf){forceSync=true;raf=requestAnimationFrame(frame);}
 }
 function seek(video,phase){
  if(video.readyState<1||video.seeking)return;
  const t=Math.min(media.controlled.duration-1/media.controlled.fps,phase*media.controlled.duration);
  if(Math.abs(video.currentTime-t)>1/(media.controlled.fps*1.8))video.currentTime=t;
 }
 function requestPlay(video){
  if(playBlocked||!video.paused||playing===video)return;
  playing=video;
  const attempt=video.play();
  attempt?.then(()=>{if(playing===video)playing=null;}).catch(error=>{if(playing===video)playing=null;if(error.name==='NotAllowedError')playBlocked=true;});
 }
 function frame(now){
  raf=0;if(!allowed()||!renderer)return;
  const dt=lastRAF?Math.min((now-lastRAF)/1000,.05):0;lastRAF=now;motor.advance(dt);onSpin(Math.abs(motor.speed)/motor.rest,Boolean(motor.drag));
  const phase=phaseOf(motor.angle),backwards=motor.speed<0;
  const desired=backwards?videos[1]:videos[0];
  // Until the reverse stream is ready, the loaded forward frames remain scrubbable.
  const chosen=desired.readyState>=2?desired:videos[0],direction=chosen===videos[1]?-1:1;
  const switched=active!==chosen;
  if(switched){active?.pause();active=chosen;forceSync=true;lastTime=-1;}
  const target=direction===1?phase:phaseOf(-motor.angle),rate=Math.abs(motor.speed)*media.controlled.duration/TAU;
  // Keep the existing composite while a newly chosen stream seeks to this angle.
  if(switched&&active.readyState>=2){seek(active,target);forceSync=false;}
  const scrub=Boolean(motor.drag)||rate<.15||chosen!==desired;
  if(active.readyState>=2&&!active.seeking&&(active!==lastVideo||active.currentTime!==lastTime)){
   try{
    const shown=direction===1?active.currentTime/media.controlled.duration:phaseOf(-active.currentTime/media.controlled.duration*TAU);
    if(!renderer.draw(active,shown)){fallback();return;}
    lastVideo=active;lastTime=active.currentTime;
    if(!framesShown){framesShown=true;host.classList.add('disco-interactive');host.setAttribute('aria-label','Disco ball. Brush your cursor across it, or grab and drag to spin. Arrow keys fling; Escape returns to cruising speed.');}
    host.classList.add('disco-controlled');
   }catch{fallback();return;}
  }
  if(scrub){active.pause();playing=null;seek(active,target);}
  else if(active.readyState>=2){
   // Native decoding handles high-speed playback; no hundreds-of-frames RAM cache.
   try{active.playbackRate=clamp(rate,.15,14);}catch{active.pause();seek(active,target);}
   const actual=active.currentTime/media.controlled.duration,difference=Math.abs(((actual-target+.5)%1+1)%1-.5);
   if(forceSync||difference>.055){seek(active,target);forceSync=false;}
   requestPlay(active);
  }
  raf=requestAnimationFrame(frame);
 }
 function load(){
  loading=true;
  if(!media.controlled){fallback();return;}
  renderer=createDiscoRenderer(host,media.controlled.size);if(!renderer){fallback();return;}
  motor.rest=TAU/media.controlled.restCycleSeconds;
  for(const key of ['forward','reverse']){
   const video=document.createElement('video');video.muted=true;video.defaultMuted=true;video.loop=true;video.playsInline=true;video.preload=key==='forward'?'auto':'metadata';
   video.setAttribute('playsinline','');video.setAttribute('aria-hidden','true');video.tabIndex=-1;video.disablePictureInPicture=true;
   video.className='disco-decode';video.src=media.controlled[key];host.append(video);videos.push(video);
   listen(video,'loadeddata',()=>{forceSync=true;sync();});
   listen(video,'error',()=>{if(key==='forward')fallback();});video.load();
  }
  listen(renderer.canvas,'webglcontextlost',event=>{event.preventDefault();fallback();});
  // This metadata also supplies the ball outline; no page-wide light layer is created.
  fetch(media.controlled.lightSamples,{signal:lifetime.signal}).then(r=>r.ok?r.json():null).then(samples=>{if(!failed&&!disposed){renderer?.setOutlines(samples?.outlines);sync();}}).catch(()=>{});
  raf=requestAnimationFrame(frame);
 }
 listen(host,'pointerenter',event=>{if(videos[1])videos[1].preload='auto';if(event.pointerType==='mouse')hover={x:event.clientX,time:event.timeStamp,distance:0,started:false};});
 listen(host,'pointerleave',()=>{if(!dragging&&hover?.started&&hover.distance>24)onRelease(Math.abs(motor.speed)/motor.rest);hover=null;});
 listen(host,'pointerdown',event=>{
  host.classList.add('pointer-focus');
  hover=null;
  playBlocked=false;
  if(event.button!==0||!event.isPrimary||!motor||!allowed())return;
  // Capture the whole generous target immediately; diagonal swipes no longer cancel a grab.
  dragging={id:event.pointerId,x:event.clientX,y:event.clientY,last:event.clientX,time:event.timeStamp,width:host.getBoundingClientRect().width,claimed:false};
  suppressClick=false;motor.grab(event.clientX,event.timeStamp,dragging.width);host.setPointerCapture?.(event.pointerId);host.focus({preventScroll:true});host.classList.add('is-grabbing');pauseVideos();host.classList.add('has-spun');onGrab();event.preventDefault();if(videos[1])videos[1].preload='auto';
 });
 listen(host,'pointermove',event=>{
  const d=dragging;
  if(!d){
   if(event.pointerType!=='mouse'||event.buttons||!allowed()||!framesShown)return;
   if(motor.flingPriority){hover=null;return;}
   if(!hover){hover={x:event.clientX,time:event.timeStamp,distance:0,started:false};return;}
   const dx=event.clientX-hover.x,elapsed=event.timeStamp-hover.time;hover.distance+=Math.abs(dx);
   if(hover.distance>12){if(!hover.started){hover.started=true;onBrush();}motor.brush(dx,elapsed,host.getBoundingClientRect().width);}
   hover.x=event.clientX;hover.time=event.timeStamp;return;
  }
  if(d.id!==event.pointerId)return;
  const dx=event.clientX-d.x,dy=event.clientY-d.y;
  if(!d.claimed){
   if(Math.hypot(dx,dy)<3)return;
   d.claimed=true;
  }
  event.preventDefault();motor.move(event.clientX,event.timeStamp);suppressClick=true;
 });
 function release(event){
  const d=dragging;if(!d||d.id!==event.pointerId)return;
  const cancelled=event.type!=='pointerup';const moved=cancelled?(motor.reset(),false):motor.release(event.timeStamp);forceSync=true;
  if(!cancelled&&(d.claimed||moved))onRelease(Math.abs(motor.speed)/motor.rest);
  dragging=null;host.classList.remove('is-grabbing');if(host.hasPointerCapture?.(event.pointerId))host.releasePointerCapture(event.pointerId);
 }
 for(const event of ['pointerup','pointercancel','lostpointercapture'])listen(host,event,release);
 listen(host,'click',event=>{if(suppressClick){suppressClick=false;event.preventDefault();event.stopImmediatePropagation();}},true);
 listen(host,'dragstart',event=>event.preventDefault());
 listen(host,'keydown',event=>{
  host.classList.remove('pointer-focus');
  if(!motor||!allowed())return;
  if(event.key==='ArrowLeft'||event.key==='ArrowRight'){event.preventDefault();motor.boost(event.key==='ArrowLeft'?-1:1);forceSync=true;onRelease(Math.abs(motor.speed)/motor.rest);}
  else if(event.key==='Escape'){event.preventDefault();motor.reset();forceSync=true;announce('');}
 });
 listen(host,'blur',()=>host.classList.remove('pointer-focus'));
 listen(motion,'change',sync);if(connection?.addEventListener)listen(connection,'change',sync);listen(document,'visibilitychange',sync);
 listen(window,'pagehide',()=>{cancelAnimationFrame(raf);raf=0;lastRAF=0;pauseVideos();motor?.reset();});listen(window,'pageshow',sync);
 if('IntersectionObserver' in window){observer=new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;sync();},{rootMargin:'40px'});observer.observe(host);}
 fetch('/assets/home-disco/disco-motion.json',{signal:lifetime.signal}).then(r=>r.ok?r.json():null).then(config=>{if(!config||disposed)return;media=config;sync();}).catch(()=>{});
 return{boost(){playBlocked=false;if(motor&&allowed()){motor.boost();forceSync=true;}},destroy(){disposed=true;lifetime.abort();observer?.disconnect();cancelAnimationFrame(raf);pauseVideos();renderer?.destroy();for(const video of videos){video.removeAttribute('src');video.load();video.remove();}host.classList.remove('disco-controlled','disco-interactive','is-grabbing');}};
}
