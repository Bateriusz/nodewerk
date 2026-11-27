// Original procedural graphics for Neon Breach. No character, footage, or font
// asset is loaded. Analytic screen primitives are NOT experimental SDF nodes.
static const float3 CYAN = float3(.10,.90,1.0);
static const float3 ROSE = float3(1.0,.16,.42);
static const float3 JADE = float3(.35,1.0,.61);
static const float3 AMBER = float3(1.0,.65,.25);
static const float3 INK = float3(.018,.024,.07);

float disc(float2 p, float r) { return 1.0-smoothstep(r-.006,r+.006,length(p)); }
float rect(float2 p, float2 s) {
    float2 d=abs(p)-s; return 1.0-smoothstep(-.004,.003,max(d.x,d.y));
}
float ring(float2 p, float r, float w) { return 1.0-smoothstep(w,w+.004,abs(length(p)-r)); }
float trace(float2 p, float2 a, float2 b, float r) {
    float2 v=b-a; float h=saturate(dot(p-a,v)/max(dot(v,v),.00001));
    return disc(p-a-v*h,r);
}
float hash21(float2 p) { return frac(sin(dot(p,float2(27.618,93.61)))*4371.213); }
float sparkle(float2 p,float scale,float t) {
    float2 cell=floor(p*scale), local=frac(p*scale)-.5;
    float pick=hash21(cell);
    return step(.95,pick)*disc(local,.035+.04*pick)*(.62+.38*sin(1.9*t+29.0*pick));
}
void ink(inout float3 c,float mask,float3 tone){ c=lerp(c,tone,saturate(mask)); }
float hashScan(float2 p,float t) { return .55+.45*sin(p.y*370.0+t*8.0); }
float ease(float a,float b,float v){ return smoothstep(a,b,v); }

// Helmets, shoulders, visors, arms and planted boots remain readable in silhouette.
void pilot(inout float3 c,float2 p,float2 origin,float scale,float pose,float3 neon,bool hero) {
    float2 q=(p-origin)/scale;
    float head=disc((q-float2(0,.54))*float2(1.0,1.06),.18);
    float chest=rect(q-float2(0,.16),float2(.19,.19));
    float neck=rect(q-float2(0,.35),float2(.10,.08));
    float2 leading=float2(.50,.10+pose*.45), trailing=float2(-.30,-.05);
    float2 forwardFoot=float2(.12+pose*.42,-.53+.44*pose);
    float arm=trace(q,float2(.15,.26),leading,.075)+trace(q,float2(-.15,.24),trailing,.071);
    float legs=trace(q,float2(.11,.01),forwardFoot,.080)+trace(q,float2(-.12,.01),float2(-.15,-.56),.08);
    float body=saturate(head+chest+neck+arm+legs);
    ink(c,body,lerp(float3(.027,.035,.080),neon*.48,saturate(.18+q.x*.8)));
    float visor=rect(q-float2(.024,.555),float2(.117,.037))*head;
    ink(c,visor,neon);
    c+=neon*.55*rect(q-float2(.02,.15),float2(.10,.016))*chest;
    if(hero) {
       float cape=step(-.62+q.y*.72,q.x)*step(q.x,-.17)*step(-.26,q.y)*step(q.y,.36);
       ink(c,cape*(1.0-body),ROSE*.39);
       ink(c,rect(q-leading-float2(.038,.03),float2(.11,.043)),neon*.65);
    }
}

float3 orbital(float2 p,float seconds,float motion){
    float3 c=INK+float3(.05,.04,.14)*exp(-3.0*abs(p.y+.28));
    c+=sparkle(p,22,seconds)*.9+sparkle(p+8.2,32,seconds)*.37;
    float2 pl=p-float2(.80,-.42);
    ink(c,disc(pl,.65),float3(.045,.058,.17)+ROSE*.26*saturate(pl.y+.48));
    c+=ROSE*.32*ring(pl,.65,.010);
    float2 ship=p-float2(-.42,.29+.018*sin(motion*.6));
    ink(c,disc(ship*float2(.53,2.2),.49),float3(.05,.09,.16));
    ink(c,rect(ship-float2(.07,-.11),float2(.76,.049)),float3(.13,.27,.42));
    c+=AMBER*.70*step(.73,frac((ship.x+.76)*9.0))*rect(ship-float2(.045,-.091),float2(.63,.023));
    // Below the orbit window: the teleport dais and the silhouetted pilot.
    ink(c,rect(p-float2(0,-.37),float2(1.9,.25)),float3(.035,.045,.115));
    c+=CYAN*.55*trace(p,float2(-1.5,-.18),float2(1.5,-.18),.007);
    c+=CYAN*.7*ring((p-float2(0,-.57))*float2(1.0,3.0),.35,.010);
    pilot(c,p,float2(0,-.37),.39,0.0,CYAN,true);
    return c;
}

float3 hotel(float2 p,uint tick,float seconds,float motion,float grain){
    float doorTime=StorySeconds(CUE_DOOR_IMPACT);
    float escapeTime=StorySeconds(CUE_ESCAPE);
    float3 c=float3(.085,.045,.165)*(.80+.20*grain);
    float depth=max(.10, abs(p.y+.03));
    float2 floorGrid=float2(frac(p.x*(.75/depth)),frac(.8/depth));
    c+=ROSE*.23*step(.96,floorGrid.x)*step(p.y,-.13);
    c+=CYAN*.20*step(.96,floorGrid.y)*step(p.y,-.13);
    float2 window=p-float2(-.95,.25);
    float inside=rect(window,float2(.55,.28));
    float2 skyline=float2(frac(window.x*10.0),window.y+.23);
    float lane=hash21(float2(floor(window.x*10.0),3.0));
    float city=step(skyline.y,(.2+.65*lane)*.47)*step(.14,skyline.x);
    float lit=step(.78,frac(window.x*65.0))*step(.72,frac(window.y*58.0));
    ink(c,inside,INK+ROSE*.17*(1.0-city)+CYAN*.68*city*lit);
    c+=AMBER*.40*(rect(window,float2(.56,.29))-inside);
    float2 door=p-float2(.67,.08);
    c+=ROSE*.72*(rect(door,float2(.35,.49))-rect(door,float2(.326,.466)));
    float opened=ease(doorTime,doorTime+.55,seconds);
    float2 panel=door-float2(.47*opened,.04*opened);
    panel.x+=(p.y-.08)*.32*opened;
    ink(c,rect(panel,float2(.31*(1.0-.68*opened),.46)),float3(.18,.10,.26));
    c+=CYAN*.6*rect(panel-float2(.20*(1.0-opened),-.10),float2(.055,.014));
    c+=ROSE*.25*exp(-7.0*abs(door.x+.36))*step(door.y,.49)*step(-.49,door.y);
    float kick=1.0-ease(.04,.53,abs(seconds-doorTime));
    float approach=ease(doorTime-3.0,doorTime,seconds);
    float engage=ease(19.0,22.0,motion);
    float2 actor=float2(lerp(-.82,.27,approach)-.58*engage,-.23+.018*sin(14.0*motion));
    float pose=max(kick,.42*engage);
    if(tick<CUE_EXIT) pilot(c,p,actor,.47,pose,CYAN,true);

    if(seconds>=doorTime+.5){
        float impactStart=StorySeconds(CUE_IMPACT_ONE);
        float impactEnd=StorySeconds(CUE_IMPACT_TWO);
        float f1=ease(impactStart,impactStart+1.0,motion);
        float f2=ease(impactEnd,impactEnd+1.3,seconds);
        float2 a=float2(.88+f1*.22,-.26-f1*.33);
        float2 b=float2(1.34+f2*.19,-.24-f2*.38);
        pilot(c,p,a,.42,.10*(1.0-f1),ROSE,false);
        pilot(c,p,b,.37,-.15*(1.0-f2),ROSE,false);
        // A third agent breaks away behind the heroine while she is
        // distracted by the flashes and the other two agents. He opens the
        // service hatch himself; she never turns or gestures toward him.
        const float2 hatch=float2(-1.21,-.19);
        float bolt=ease(escapeTime,escapeTime+4.25,seconds);
        float openHatch=ease(escapeTime+3.2,escapeTime+3.64,seconds)*
                        (1.0-ease(escapeTime+6.15,escapeTime+6.9,seconds));
        float2 survivor=float2(lerp(.97,-1.43,bolt),-.22+.31*sin(3.1415926*bolt));
        c+=JADE*.40*(rect(p-hatch,float2(.20,.34))-rect(p-hatch,float2(.165,.305)));
        ink(c,rect(p-hatch,float2(.16,.302))*openHatch,INK*.74);
        c+=openHatch*.55*JADE*ring((p-hatch)*float2(1.0,.48),.28,.012);
        if(seconds<escapeTime+4.55){
            float masker=(p.x<-.56 && p.y<.05) ? 1.0-openHatch : 0.0;
            float3 before=c;
            pilot(c,p,survivor,.33,.05+.17*sin(33.0*motion),AMBER,false);
            c=lerp(c,before,saturate(masker*ease(escapeTime+4.33,escapeTime+4.54,seconds)));
        }
        float escape=CuePulse(tick,CUE_ESCAPE);
        float exitCue=CuePulse(tick,CUE_EXIT);
        c+=.49*AMBER*escape*ring(p-survivor,.30,.014);
        c+=ROSE*.12*ease(impactEnd,impactEnd+.5,seconds)*
           (1.0-ease(escapeTime+2.0,escapeTime+3.4,seconds))*
           (.55+.45*sin(11.0*seconds))*rect(p-float2(.63,.57),float2(.42,.033));
        c+=.77*JADE*exitCue*rect(p-hatch,float2(.215,.015));
    }
    if(seconds >= doorTime-.04 && seconds < doorTime+1.0){
        float debrisT=max(0.0,seconds-doorTime);
        [unroll] for(uint i=0;i<9;i++){
            float j=float(i);
            float2 debris=float2(.67,.08)+debrisT*float2(.3+.06*j,.57-.14*j);
            c+=AMBER*.75*rect(p-debris,float2(.025,.011))*(1.0-saturate(debrisT));
        }
    }
    if(tick>=CUE_SHOT_ONE && seconds<StorySeconds(CUE_IMPACT_TWO)+.4) {
        float rays=max(CuePulse(tick,CUE_SHOT_ONE),CuePulse(tick,CUE_SHOT_TWO));
        float impactA=CuePulse(tick,CUE_IMPACT_ONE);
        float impactB=CuePulse(tick,CUE_IMPACT_TWO);
        float2 hit=tick<CUE_SHOT_TWO ? float2(.93,-.03) : float2(1.43,-.02);
        float2 emitter=actor+float2(.24,.14);
        c+=CYAN*rays*(1.4*trace(p,emitter,hit,.025)+.62*trace(p,emitter,hit,.085));
        c+=1.1*ROSE*(impactA+impactB)*ring(p-hit,.13,.030);
    }
    if(tick>=CUE_EXIT){
        // Cutaway: the amber agent is already OUTSIDE, left of the sealed
        // threshold. This invented energy pulse is stylized, not a mechanism.
        float3 corridor=INK+float3(.04,.10,.14)*saturate(.7-p.y);
        ink(c,step(p.x,-.20),corridor);
        c+=JADE*.70*trace(p,float2(-.20,-.66),float2(-.20,.71),.018);
        c+=JADE*.39*rect(p-float2(-.64,.67),float2(.36,.023));
        float draw=ease(StorySeconds(CUE_EXIT)+.30,StorySeconds(CUE_DISRUPTOR),seconds);
        float2 agent=float2(-.91,-.26);
        pilot(c,p,agent,.50,draw,AMBER,false);
        float2 emitter=agent+float2(.25,.05+.225*draw);
        float3 ultraviolet=float3(.71,.32,1.0);
        c+=ultraviolet*draw*.72*disc(p-emitter,.034);
        float stunned=ease(StorySeconds(CUE_STUN),StorySeconds(CUE_STUN)+.75,seconds);
        float2 target=float2(.64,-.17-.39*stunned);
        float2 pivot=p-target;
        float angle=stunned*1.42;
        float2 lean=float2(cos(angle)*pivot.x-sin(angle)*pivot.y,
                           sin(angle)*pivot.x+cos(angle)*pivot.y);
        pilot(c,lean,float2(0.0,0.0),.48,0.0,lerp(CYAN,INK,stunned*.76),true);
        float fire=CuePulse(tick,CUE_DISRUPTOR);
        float stun=CuePulse(tick,CUE_STUN);
        c+=ultraviolet*fire*(1.15*trace(p,emitter,target,.035)+.68*ring(p-emitter,.13,.018));
        if(tick>=CUE_DISRUPTOR && tick<CUE_STUN){
            float flight=saturate(float(tick-CUE_DISRUPTOR)/float(CUE_STUN-CUE_DISRUPTOR));
            float2 front=lerp(emitter,target,flight);
            c+=ultraviolet*(.87*ring(p-front,.11,.024)+1.1*disc(p-front,.045));
        }
        c+=ultraviolet*stun*(.87*ring(p-target,.23,.031)+.29*disc(p-target,.18));
    }

    return c;
}

float4 NeonBreach(float2 uv,uint tick,float grain) {
    float seconds=StorySeconds(tick);
    float motion=StorySeconds(StoryMotionTick(tick));
    float2 p=float2((uv.x-.5)*3.5555556,1.0-2.0*uv.y);
    float3 c=tick<CUE_TELEPORT ? orbital(p,seconds,motion) : hotel(p,tick,seconds,motion,grain);
    float tele=CuePulse(tick,CUE_TELEPORT);
    float kick=CuePulse(tick,CUE_DOOR_IMPACT);
    float hit=max(CuePulse(tick,CUE_IMPACT_ONE),CuePulse(tick,CUE_IMPACT_TWO));
    float shot=max(CuePulse(tick,CUE_SHOT_ONE),CuePulse(tick,CUE_SHOT_TWO));
    c+=tele*(.6*CYAN+1.2*CYAN*ring(p-float2(-.77,-.14),.45,.021));
    c+=kick*.22*AMBER+hit*.19*ROSE;
    c*=.83+.17*hashScan(p,motion);
    c*=saturate(1.15-.17*dot(p,p));

    float3 printColor=tick<CUE_TELEPORT ? CYAN : (tick<CUE_ESCAPE ? ROSE : JADE);
    uint title=0u;
    if(tick>=CUE_TELEPORT) title=1u;
    if(tick>=CUE_DOOR_IMPACT) title=2u;
    if(tick>=STORY_SLOW) title=3u;
    if(tick>=STORY_FREEZE) title=4u;
    if(tick>=STORY_REVERSE) title=5u;
    if(tick>=STORY_FORWARD) title=6u;
    if(tick>=CUE_ESCAPE) title=7u;
    if(tick>=CUE_EXIT) title=8u;
    if(tick>=CUE_DISRUPTOR) title=9u;
    if(tick>=CUE_STUN) title=10u;
    float band=step(uv.y,.130)+step(.867,uv.y);
    ink(c,band*.88,INK);
    c+=printColor*.85*StoryText(uv,float2(.040,.036),0u);
    c+=printColor*.95*StoryText(uv,float2(.040,.902),title+1u);
    c+=printColor*.27*rect(uv-float2(.50,.135),float2(.46,.002));
    c+=printColor*.27*rect(uv-float2(.50,.862),float2(.46,.002));
    c+=printColor*.60*rect(uv-float2(.045+.445*saturate(float(tick)/float(STORY_DURATION)),.96),
                          float2(.445*saturate(float(tick)/float(STORY_DURATION)),.005));
    if(tick>=CUE_FINALE){
        c=INK+float3(.078,.024,.16)*exp(-2.1*dot(p,p));
        c+=ROSE*.30*ring(p,.63,.013);
        c+=CYAN*.21*rect(uv-float2(.50,.18),float2(.43,.004));
        [unroll] for (uint rowIndex=0u;rowIndex<4u;++rowIndex){
            c+=lerp(CYAN,ROSE,float(rowIndex)/4.0)*
                StoryText(uv,float2(.12,.29+.12*float(rowIndex)),STORY_FINALE_TEXT+rowIndex);
        }
    }
    if(uv.x<.026 && uv.y<.023) { // diegetic timing LED, driven by the same cues
        float led=(tele+kick+hit+shot+max(CuePulse(tick,CUE_ESCAPE),CuePulse(tick,CUE_EXIT))+
                   max(CuePulse(tick,CUE_DISRUPTOR),max(CuePulse(tick,CUE_STUN),
                   CuePulse(tick,CUE_FINALE))))>0.0 ? 1.0 : 0.0;
        return float4(led,0.0,0.0,1.0);
    }
    return float4(saturate(c),1.0);
}
