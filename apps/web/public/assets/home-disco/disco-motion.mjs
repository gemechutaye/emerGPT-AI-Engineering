// One cycle is one pass through the approved Runway frames.
export const TAU=Math.PI*2;
export const phaseOf=angle=>((angle/TAU)%1+1)%1;
export const clamp=(value,low,high)=>Math.max(low,Math.min(high,value));
export class DiscoMotor{
 constructor(cycleSeconds=7.2){this.rest=TAU/cycleSeconds;this.speed=this.rest;this.angle=0;this.drag=null;this.flingPriority=false;this.decay=.65;this.limit=TAU*1.4;}
 advance(seconds){
  if(seconds<=0)return;
  if(this.drag){
   // A damped grip applies torque; grabbing never teleports or instantly reverses the rotor.
   const d=this.drag,steps=Math.ceil(seconds*120),dt=seconds/steps;
   for(let i=0;i<steps;i++){
    d.idle+=dt;const hand=d.velocity*Math.exp(-Math.max(0,d.idle-.025)*24);
    const torque=80*(d.target-this.angle)+10*(hand-this.speed);
    this.speed=clamp(this.speed+torque*dt,-this.limit,this.limit);this.angle+=this.speed*dt;
   }
   return;
  }
  if(Math.abs(this.speed-this.rest)<this.rest*.45)this.flingPriority=false;
  const extra=this.speed-this.rest,e=Math.exp(-this.decay*seconds);
  this.angle+=this.rest*seconds+extra*(1-e)/this.decay;
  this.speed=this.rest+extra*e;
 }
 grab(x,time,width){this.drag={x,time,width:Math.max(1,width),velocity:0,distance:0,target:this.angle,idle:0};}
 move(x,time){
  if(!this.drag)return;
  const d=this.drag,dx=x-d.x,dt=Math.max(.008,(time-d.time)/1000),angle=dx/(d.width*.85)*Math.PI;
  d.target+=angle;d.velocity=clamp(d.velocity*.2+angle/dt*.8,-this.limit*1.2,this.limit*1.2);
  d.distance+=Math.abs(dx);d.x=x;d.time=time;d.idle=0;
 }
 release(time){
  if(!this.drag)return false;
  const d=this.drag,moved=d.distance>5;
  if(moved)this.speed=clamp((this.speed*.18+d.velocity*.82)*Math.exp(-Math.max(0,time-d.time-35)/120),-this.limit,this.limit);
  this.flingPriority=moved;this.drag=null;return moved;
 }
 boost(direction=1){this.flingPriority=true;this.speed=clamp(this.speed+direction*this.rest*5,-this.limit,this.limit);}
 brush(dx,milliseconds,width){
  if(this.drag||this.flingPriority||!dx||milliseconds<=0||milliseconds>160)return;
  const hand=clamp(dx/Math.max(1,width)/Math.max(.008,milliseconds/1000)*Math.PI,-this.limit,this.limit);
  const grip=Math.min(.26,Math.abs(dx)/Math.max(1,width)*3);
  this.speed=clamp(this.speed+(hand-this.speed)*grip,-this.limit,this.limit);
 }
 reset(){this.drag=null;this.flingPriority=false;this.speed=this.rest;}
}
// Reflect a fixed light about a rotating mirror normal, then intersect the
// outgoing ray with a page plane behind the ball. Coordinates are in ball radii.
export function reflectionOnPage(normal,light,depth=2.8){
 const length=Math.hypot(...light),l=light.map(v=>v/length),dot=normal.reduce((s,n,i)=>s+n*l[i],0);
 if(normal[2]<.04||dot<=0)return null;
 const ray=normal.map((n,i)=>2*dot*n-l[i]);
 if(ray[2]>=-.09)return null;
 const travel=(-depth-normal[2])/ray[2];
 return {x:normal[0]+ray[0]*travel,y:normal[1]+ray[1]*travel,travel,angle:Math.atan2(ray[1],ray[0]),normal};
}
export function reflectionTiles(phase){
 const result=[];
 for(let row=0;row<4;row++){
  const latitude=-.62+row*.36,y=Math.sin(latitude),radius=Math.cos(latitude);
  for(let tile=0;tile<18;tile++){
   const turn=phase*TAU+tile/18*TAU+row*.11;
   const normal=[Math.sin(turn)*radius,y,Math.cos(turn)*radius];
   for(const [fixture,light] of [[0,[-.65,-.9,1.65]],[1,[.9,-.5,1.5]]]){
    const ray=reflectionOnPage(normal,light);if(ray)result.push({...ray,fixture,row,tile});
   }
  }
 }
 return result;
}
