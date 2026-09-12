// Tom Kincaid / VOLE.wtf, CC0. See assets/audio/LICENSE.md.
export const BIRTHDAY_TRACK={
 url:new URL('./audio/happy-birthday-jazz.mp3',import.meta.url).href,
 start:.65,
 end:25.95
};

export async function loadBirthdayTrack(ctx,signal){
 const response=await fetch(BIRTHDAY_TRACK.url,{signal});
 if(!response.ok)throw new Error('Birthday recording unavailable');
 return ctx.decodeAudioData(await response.arrayBuffer());
}

export function playBirthdayTrack(ctx,output,buffer,offset=BIRTHDAY_TRACK.start){
 const source=ctx.createBufferSource();
 source.buffer=buffer;source.loop=true;
 source.loopStart=BIRTHDAY_TRACK.start;source.loopEnd=Math.min(BIRTHDAY_TRACK.end,buffer.duration);
 source.connect(output);
 source.onended=()=>source.disconnect();
 source.start(0,Math.max(source.loopStart,Math.min(offset,source.loopEnd)));
 return source;
}
