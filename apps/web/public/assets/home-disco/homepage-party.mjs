import {initDisco} from './disco.mjs';
import {createConfetti} from './confetti.mjs';
import {BIRTHDAY_TRACK,loadBirthdayTrack,playBirthdayTrack} from './music-track.mjs';
import {createUISounds} from './ui-sounds.mjs';

export function mountParty(host,button,status){
 const lifetime=new AbortController();
 let disposed=false,ctx=null,track=null,buffer=null,loading=null,synth=null,musicBus=null,wanted=false,muted=false,position=BIRTHDAY_TRACK.start,startedAt=0;
 let lastTwirled=0,lastPaper=0;
 const label=(text,on=false)=>{button.textContent=text;button.setAttribute('aria-pressed',String(on));};
 const paper=createConfetti(host);
 function stop(){
  loading?.abort();loading=null;
  if(track){const length=track.loopEnd-track.loopStart;position=track.loopStart+(position-track.loopStart+ctx.currentTime-startedAt)%length;track.stop();track.disconnect();track=null;}
  if(ctx){const previous=ctx;ctx=null;void previous.close().catch(()=>{});}
  synth=null;musicBus=null;
 }
 async function start(){
  if(disposed||muted||document.hidden||track||loading)return;
  const Audio=window.AudioContext||window.webkitAudioContext;
  if(!Audio){label('Sound unavailable');return;}
  wanted=true;const controller=new AbortController();loading=controller;
  try{
   ctx??=new Audio();const audio=ctx;
   await audio.resume();if(disposed||controller.signal.aborted)return;
   label('Loading sound…');
   const decoded=buffer||await loadBirthdayTrack(audio,controller.signal);
   if(disposed||controller.signal.aborted||ctx!==audio)return;
   buffer=decoded;
   const master=audio.createGain();master.gain.value=.14;master.connect(audio.destination);
   musicBus=audio.createGain();musicBus.gain.value=1.8;musicBus.connect(master);
   synth=createUISounds(audio,master);
   track=playBirthdayTrack(audio,musicBus,buffer,position);startedAt=audio.currentTime;
   label('Sound on',true);status.textContent='';
  }catch(error){
   if(!disposed&&!controller.signal.aborted){stop();label('Retry sound');status.textContent='Music could not load. Try sound again.';}
  }finally{if(loading===controller)loading=null;}
 }
 function celebrate(){
  if(!synth||!ctx)return;
  const now=ctx.currentTime,gain=musicBus.gain;
  gain.cancelScheduledValues(now);gain.setValueAtTime(gain.value,now);gain.linearRampToValueAtTime(1.8*.45,now+.025);gain.linearRampToValueAtTime(1.8,now+.5);
  [72,76,79,84].forEach((note,i)=>synth.tone(note,now+.005+i*.065,.24,'triangle',.4));
 }
 const paperFromSides=count=>paper.burst({small:true,count,nearBall:true});
 const disco=initDisco(host,{
  onGrab(){void start();lastTwirled=0;},
  onBrush(){lastTwirled=0;},
  onRelease(speed){
   void start();
   if(speed>=2&&synth&&ctx){const base=60+[0,2,4,7,9][Math.round(speed)%5];synth.tone(base,ctx.currentTime+.004,.075,'sine',.85);synth.tone(72,ctx.currentTime+.004,.028,'triangle',.15);}
   if(speed<2)return;lastTwirled=performance.now();lastPaper=lastTwirled;paperFromSides(Math.min(80,30+Math.round(speed*5)));
  },
  onSpin(speed,held){const now=performance.now();if(held||!lastTwirled||speed<2.5||now-lastTwirled>10000||now-lastPaper<800)return;lastPaper=now;paperFromSides(Math.min(28,12+Math.round(speed*2)));}
 });
 host.addEventListener('click',()=>{void start();lastTwirled=0;paperFromSides(28);celebrate();disco.boost();},{signal:lifetime.signal});
 button.addEventListener('click',()=>{
  if(track||loading){wanted=false;muted=true;stop();label('Sound off');status.textContent='';}
  else{muted=false;void start();}
 },{signal:lifetime.signal});
 document.addEventListener('visibilitychange',()=>{
  if(document.hidden){stop();label(wanted?'Tap for sound':'Sound off');}
  else if(wanted)void start();
 },{signal:lifetime.signal});
 window.addEventListener('pagehide',stop,{signal:lifetime.signal});
 label('Tap for sound');
 return ()=>{disposed=true;lifetime.abort();disco.destroy();paper.destroy();stop();};
}
