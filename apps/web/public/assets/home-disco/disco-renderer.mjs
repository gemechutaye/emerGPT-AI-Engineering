// Restore RGB + alpha from one packed video. This is a flat compositing quad,
// not a replacement sphere, photo warp, or CSS rotation of the original image.
export function createDiscoRenderer(host,size){
 let outlines=null;
 const canvas=document.createElement('canvas');canvas.width=size;canvas.height=size;canvas.className='disco-frame';canvas.setAttribute('aria-hidden','true');
 const gl=canvas.getContext('webgl',{alpha:true,premultipliedAlpha:true,antialias:false,depth:false,powerPreference:'low-power'});
 if(!gl)return null;
 function shader(type,source){const s=gl.createShader(type);gl.shaderSource(s,source);gl.compileShader(s);if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error('Disco compositor unavailable');return s;}
 try{
  const program=gl.createProgram();
  gl.attachShader(program,shader(gl.VERTEX_SHADER,'attribute vec2 p; varying vec2 uv; void main(){gl_Position=vec4(p,0.,1.);uv=vec2((p.x+1.)*.5,(1.-p.y)*.5);}'));
  gl.attachShader(program,shader(gl.FRAGMENT_SHADER,`precision mediump float;
   varying vec2 uv; uniform sampler2D frame; uniform vec4 outline;
   vec3 colorAt(vec2 p){return texture2D(frame,vec2(p.x,p.y*.5)).rgb;}
   void main(){
    vec2 q=clamp(uv,vec2(${.5/size}),vec2(${1-.5/size}));
    vec2 delta=q-outline.xy;
    float edge=(1.-length(delta/outline.zw))*min(outline.z,outline.w)*${size}.;
    float body=smoothstep(-.7,.7,edge);
    float originalAlpha=clamp((texture2D(frame,vec2(q.x,.5+q.y*.5)).r-.008)/.984,0.,1.);
    float cap=(1.-smoothstep(.205,.22,q.y))*(1.-smoothstep(.052,.062,abs(delta.x)));
    float a=max(body,originalAlpha*cap);
    vec3 rgb=colorAt(q);
    // Only the outer three texels borrow clean interior color. Mirror detail stays sharp.
    vec2 inward=q-delta/max(length(delta),.001)*(3./${size}.);
    float rim=(1.-smoothstep(0.,3.,edge))*(1.-cap);
    rgb=mix(rgb,colorAt(inward),rim);
    gl_FragColor=vec4(rgb*a,a);
   }`));
  gl.linkProgram(program);if(!gl.getProgramParameter(program,gl.LINK_STATUS))throw Error('Disco compositor unavailable');gl.useProgram(program);
  const outlineLocation=gl.getUniformLocation(program,'outline');
  const buffer=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buffer);gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]),gl.STATIC_DRAW);
  const p=gl.getAttribLocation(program,'p');gl.enableVertexAttribArray(p);gl.vertexAttribPointer(p,2,gl.FLOAT,false,0,0);
  const texture=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,texture);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
  gl.viewport(0,0,size,size);host.append(canvas);
  return {canvas,setOutlines(value){if(Array.isArray(value)&&value.length)outlines=value;},draw(video,phase=0){if(gl.isContextLost())return false;const o=outlines?.[Math.floor(phase*outlines.length)%outlines.length]??[.4993,.5552,.3988,.4048];gl.uniform4fv(outlineLocation,o);gl.bindTexture(gl.TEXTURE_2D,texture);gl.texImage2D(gl.TEXTURE_2D,0,gl.RGB,gl.RGB,gl.UNSIGNED_BYTE,video);gl.drawArrays(gl.TRIANGLES,0,6);return true;},destroy(){gl.deleteTexture(texture);gl.deleteBuffer(buffer);gl.deleteProgram(program);canvas.remove();}};
 }catch{canvas.remove();return null;}
}
