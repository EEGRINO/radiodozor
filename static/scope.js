'use strict';

// Adapted from wifi-densepose/laptop_server/static/visor_grain.js.
const SignalScope = (() => {
 const trailDuration=2500, maxFrameGap=450;
 const useful=Array.from({length:52},(_,i)=>i<26?i+1:i+12);
 const clamp=(value,min,max)=>Math.min(max,Math.max(min,value));

 function decode(encoded) {
  if (typeof encoded!=='string'||encoded.length>172) return null;
  let wire;
  try {wire=atob(encoded);} catch (_) {return null;}
  if (!wire.length||wire.length>128||wire.length%2) return null;
  const samples=new Int8Array(wire.length);
  for (let i=0;i<samples.length;i++) samples[i]=wire.charCodeAt(i);
  return samples;
 }

 function normalizedIQ(samples) {
  const indexes=useful.filter(index=>2*index+1<samples.length);
  if (!indexes.length) return null;
  const carriers=indexes.map(index=>[samples[2*index+1],samples[2*index]]);
  const rms=Math.sqrt(carriers.reduce((sum,[i,q])=>sum+i*i+q*q,0)/carriers.length);
  return rms?carriers.map(([i,q])=>[i/rms,q/rms]):null;
 }

 function alignPhase(frame,reference) {
  let real=0,imag=0;
  for (let i=0;i<frame.length;i++) {
   const [x,y]=frame[i], [rx,ry]=reference[i];
   real+=x*rx+y*ry;
   imag+=y*rx-x*ry;
  }
  const strength=Math.hypot(real,imag);
  if (strength<frame.length*.25) return null;
  const cos=real/strength,sin=imag/strength;
  return frame.map(([i,q])=>[i*cos+q*sin,q*cos-i*sin]);
 }

 function history() {
  let line='', key=null, previousTime=0, previousSequence, reference=null, previous=null;
  let recorded=[];

  return {
   update(packet) {
    if (!packet||packet.key===key) return;
    const samples=normalizedIQ(packet.samples);
    if (!samples) return;
    const nextLine=JSON.stringify([packet.receiver,packet.source,packet.channel,packet.peer,packet.samples.length]);
    if (line!==nextLine) recorded=[];
    const elapsed=packet.receivedAt-previousTime;
    const sequenceReset=Number.isInteger(packet.sequence)&&Number.isInteger(previousSequence)&&
     ((packet.sequence-previousSequence)>>>0)>0x80000000;
    const continuous=line===nextLine&&elapsed>0&&elapsed<=maxFrameGap&&!sequenceReset;
    const aligned=continuous&&alignPhase(samples,reference);
    line=nextLine;key=packet.key;previousTime=packet.receivedAt;previousSequence=packet.sequence;
    if (!aligned) {
     reference=samples;
     previous=samples.map(()=>[0,0]);
     return;
    }
    const backgroundBlend=1-Math.exp(-elapsed/2700);
    const signalBlend=1-Math.exp(-elapsed/135);
    const segments=[];
    for (let i=0;i<aligned.length;i++) {
     const start=previous[i], end=[0,0];
     for (let axis=0;axis<2;axis++) {
      const delta=aligned[i][axis]-reference[i][axis];
      reference[i][axis]+=delta*backgroundBlend;
      end[axis]=start[axis]+(delta-start[axis])*signalBlend;
     }
     const shift=Math.hypot(end[0]-start[0],end[1]-start[1]);
     if (shift>=.03) segments.push({start,end,shift});
     previous[i]=end;
    }
    if (segments.length) {
     segments.sort((a,b)=>b.shift-a.shift);
     recorded.push({at:packet.receivedAt,segments:segments.slice(0,14)});
     if (recorded.length>48) recorded.shift();
    }
   },
   visible(now) {
    recorded=recorded.filter(frame=>now-frame.at<trailDuration);
    return recorded.map(frame=>({...frame,heat:clamp(1-(now-frame.at)/trailDuration,0,1)}));
   },
  };
 }

 function iqPlot() {
  let track=history(), activeLine=null;
  const palette=['#3568a0','#538bcd','#60bfe4','#b8e9ed','#ffcc83','#fff0bc'];
  return function draw(ctx,w,h,packet,gain,now,line) {
   if (line!==activeLine) {track=history();activeLine=line;}
   track.update(packet);
   const frames=track.visible(now);
   const x=w/2,y=h/2,radius=Math.min(w,h)*.39;
   function point([i,q]) {
    const scale=radius/(.50+Math.hypot(i,q));
    return [x+i*scale,y-q*scale];
   }
   ctx.save();
   ctx.fillStyle='#080e17';ctx.fillRect(0,0,w,h);
   ctx.strokeStyle='#232e42';ctx.lineWidth=1;
   for (const ratio of [.32,.65,1]) {
    ctx.beginPath();ctx.arc(x,y,ratio*radius,0,Math.PI*2);ctx.stroke();
   }
   ctx.strokeStyle='#34445a';
   ctx.beginPath();
   ctx.moveTo(x-radius,y);ctx.lineTo(x+radius,y);
   ctx.moveTo(x,y-radius);ctx.lineTo(x,y+radius);
   ctx.stroke();
   ctx.fillStyle='#a1b5cb';ctx.font='12px system-ui';ctx.textAlign='center';ctx.textBaseline='middle';
   ctx.fillText('ΔI',x+radius+15,y);
   ctx.fillText('ΔQ',x,y-radius-12);
   ctx.fillStyle='#718da7';
   ctx.beginPath();ctx.arc(x,y,2,0,Math.PI*2);ctx.fill();
   ctx.lineCap='round';
   for (const {heat,segments} of frames) {
    const color=palette[Math.floor(heat*(palette.length-.01))];
    ctx.strokeStyle=color;
    ctx.globalAlpha=clamp(gain*heat**1.6*.74,0,1);
    ctx.lineWidth=1+heat*1.3;
    ctx.beginPath();
    for (const {start,end} of segments) {
     ctx.moveTo(...point(start));ctx.lineTo(...point(end));
    }
    ctx.stroke();
    if (heat>.94) {
     ctx.fillStyle=color;
     for (const {end} of segments) {
      ctx.beginPath();ctx.arc(...point(end),2,0,Math.PI*2);ctx.fill();
     }
    }
   }
   ctx.restore();
  };
 }

 return {decode,iqPlot};
})();
