// Short feedback tones only. The background music is a recorded jazz trio.
export function createUISounds(ctx,output){
 let paper;
 function brush(intensity=.3){
  intensity=Math.max(0,Math.min(1,Number(intensity)||0));
  if(!paper){paper=ctx.createBuffer(1,Math.ceil(ctx.sampleRate*.095),ctx.sampleRate);let seed=7,soft=0;const a=paper.getChannelData(0);for(let i=0;i<a.length;i++){seed=(Math.imul(seed,1664525)+1013904223)>>>0;const noise=seed/2147483648-1;soft=.72*soft+.28*noise;a[i]=(.72*noise+.28*soft)*(.62+.38*Math.abs(Math.sin(i*.049)));}}
  const source=ctx.createBufferSource(),filter=ctx.createBiquadFilter(),gain=ctx.createGain(),time=ctx.currentTime;
  source.buffer=paper;filter.type='bandpass';filter.frequency.value=950+intensity*1300;filter.Q.value=.65;
  gain.gain.setValueAtTime(0,time);gain.gain.linearRampToValueAtTime(.22+intensity*.2,time+.004);gain.gain.exponentialRampToValueAtTime(.0001,time+.092);
  source.connect(filter);filter.connect(gain);gain.connect(output);source.onended=()=>{source.disconnect();filter.disconnect();gain.disconnect();};source.start();
 }
 function tone(note,time,duration,type='triangle',volume=.25){
  const osc=ctx.createOscillator(),gain=ctx.createGain();
  osc.type=type;osc.frequency.value=440*2**((note-69)/12);
  gain.gain.setValueAtTime(0,time);gain.gain.linearRampToValueAtTime(volume,time+.007);
  gain.gain.exponentialRampToValueAtTime(.0001,time+Math.max(.025,duration));
  osc.connect(gain);gain.connect(output);
  osc.onended=()=>{osc.disconnect();gain.disconnect();};osc.start(time);osc.stop(time+duration+.025);
 }
 // Three tiny high notes: the paper sparkle on the card and on a rare result.
 function sprinkle(time=ctx.currentTime){
  [84,91,96].forEach((note,i)=>tone(note,time+i*.045,.07,'sine',[.10,.075,.06][i]));
 }
 return {tone,brush,sprinkle};
}
