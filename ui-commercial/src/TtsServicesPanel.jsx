import React, {useEffect, useMemo, useRef, useState} from "react";

const PROVIDERS = [
  {key:"edge",name:"Edge TTS",desc:"免费在线配音，无需填写 API Key",provider:"edge_tts"},
  {key:"minimax",name:"MiniMax",desc:"中文自然度较高，支持热门音色和语音克隆",provider:"minimax"},
  {key:"bailian",name:"阿里云百炼",desc:"阿里云官方语音服务，适合中文配音",provider:"aliyun_bailian"},
  {key:"openai",name:"OpenAI 配音",desc:"使用 OpenAI 官方语音接口",provider:"openai_compatible"},
  {key:"custom",name:"自定义兼容接口",desc:"适合第三方 OpenAI 格式配音服务",provider:"openai_compatible",advanced:true},
];

const DEFAULTS = {
  edge:{name:"Edge TTS",provider:"edge_tts",base_url:"",api_key:"",model:"edge-tts",response_format:"mp3",timeout_seconds:120,is_enabled:true,is_default:true},
  minimax:{name:"MiniMax",provider:"minimax",base_url:"https://api.minimaxi.com",api_key:"",model:"speech-2.8-hd",response_format:"mp3",timeout_seconds:120,is_enabled:true,is_default:false},
  bailian:{name:"阿里云百炼",provider:"aliyun_bailian",base_url:"https://dashscope.aliyuncs.com/api/v1/services/audio/tts/SpeechSynthesizer",api_key:"",model:"qwen-audio-3.0-tts-flash",response_format:"wav",timeout_seconds:120,is_enabled:true,is_default:false},
  openai:{name:"OpenAI 配音",provider:"openai_compatible",base_url:"https://api.openai.com/v1",api_key:"",model:"tts-1",response_format:"mp3",timeout_seconds:120,is_enabled:true,is_default:false},
  custom:{name:"自定义配音服务",provider:"openai_compatible",base_url:"",api_key:"",model:"",response_format:"mp3",timeout_seconds:120,is_enabled:true,is_default:false},
};

function decodeUnicodeOnce(value){
  if(typeof value!=="string"||!value.includes("\\u"))return value;
  return value.replace(/\\u([0-9a-fA-F]{4})/g,(_,hex)=>String.fromCharCode(parseInt(hex,16)));
}
function decodePayload(value){
  if(Array.isArray(value))return value.map(decodePayload);
  if(value&&typeof value==="object")return Object.fromEntries(Object.entries(value).map(([key,item])=>[key,decodePayload(item)]));
  return decodeUnicodeOnce(value);
}
async function api(path,options={}){
  const response=await fetch(path,{headers:{"Content-Type":"application/json"},...options,body:options.body&&typeof options.body!=="string"?JSON.stringify(options.body):options.body});
  const payload=decodePayload(await response.json());
  if(!payload.ok)throw new Error(payload.error||"操作失败");
  return payload.data;
}
function Button({children,primary=false,danger=false,disabled=false,onClick}){return <button type="button" className={`button ${primary?"primary":"quiet"} ${danger?"danger":""}`} disabled={disabled} onClick={onClick}><span>{children}</span></button>}
function Field({label,wide=false,children,hint}){return <label className={wide?"wide":""}><span>{label}</span>{children}{hint&&<small>{hint}</small>}</label>}
function friendlyError(error){
  const message=decodeUnicodeOnce(String(error?.message||error||""));
  if(/401|403|invalid[_ ]?api[_ ]?key|unauthorized/i.test(message))return "API Key 无效";
  if(/429|rate.?limit|频率|thrott/i.test(message))return "请求频率受限，请稍后重试";
  if(/voice.*(not|invalid)|音色.*(不存在|不匹配)/i.test(message))return "音色不存在或与当前模型不匹配";
  if(/model.*(not|invalid|unavailable)|模型.*(不存在|不可用)/i.test(message))return "模型不可用";
  if(/refused|timed? ?out|urlopen|连接|network|fetch/i.test(message))return "网络连接失败或接口地址无法访问";
  return message||"配音服务测试失败";
}

export default function TtsServicesPanel({db,refresh,notify}){
  const [expanded,setExpanded]=useState(()=>localStorage.getItem("mixcut.settings.tts.expanded")==="1");
  const [open,setOpen]=useState(false);
  const [kind,setKind]=useState("edge");
  const [form,setForm]=useState(DEFAULTS.edge);
  const [busy,setBusy]=useState("");
  const [listenText,setListenText]=useState("你好 这是 AI 配音试听");
  const [voicesOpen,setVoicesOpen]=useState(false);
  const [cloneOpen,setCloneOpen]=useState(false);
  const [revealed,setRevealed]=useState({});
  const [voiceDraft,setVoiceDraft]=useState({name:"",voice_code:"",is_default:false});
  const [clone,setClone]=useState({name:"",sample_path:"",voice_id:"",preview_text:""});
  const [extra,setExtra]=useState({});
  const routeHandled=useRef(false);

  useEffect(()=>setExtra({
    default_tts_voice_id:db.settings.default_tts_voice_id||"",
    tts_speed:db.settings.tts_speed||"1",
    tts_pitch:db.settings.tts_pitch||"0",
    tts_volume:db.settings.tts_volume||db.settings.voice_volume||"1",
    minimax_group_id:db.settings.minimax_group_id||"",
    aliyun_bailian_sample_rate:db.settings.aliyun_bailian_sample_rate||"24000",
  }),[db.settings]);

  const isOfficialOpenAI=service=>service.provider==="openai_compatible"&&(String(service.base_url||"").includes("api.openai.com")||String(service.name||"").toLowerCase().includes("openai"));
  const serviceFor=key=>{
    if(key==="edge")return db.tts_services.find(item=>item.provider==="edge_tts");
    if(key==="minimax")return db.tts_services.find(item=>item.provider==="minimax");
    if(key==="bailian")return db.tts_services.find(item=>item.provider==="aliyun_bailian");
    if(key==="openai")return db.tts_services.find(isOfficialOpenAI);
    return db.tts_services.find(item=>item.provider==="openai_compatible"&&!isOfficialOpenAI(item));
  };
  const voices=useMemo(()=>db.tts_voices.filter(item=>Number(item.service_id)===Number(form.id)),[db.tts_voices,form.id]);
  const currentService=db.tts_services.find(item=>item.is_default&&item.is_enabled)||db.tts_services.find(item=>item.is_enabled)||serviceFor("edge");
  const currentVoice=db.tts_voices.find(item=>String(item.id)===String(extra.default_tts_voice_id))||db.tts_voices.find(item=>Number(item.service_id)===Number(currentService?.id)&&item.is_default);
  const currentConfigured=currentService?.provider==="edge_tts"||Boolean(currentService?.api_key_configured);

  const setPanelExpanded=value=>{setExpanded(value);localStorage.setItem("mixcut.settings.tts.expanded",value?"1":"0")};
  const configure=(providerKind,pushRoute=true)=>{
    const service=serviceFor(providerKind);
    setKind(providerKind);setForm({...DEFAULTS[providerKind],...(service||{}),api_key:""});
    setVoicesOpen(false);setCloneOpen(false);setOpen(true);
    if(pushRoute)history.pushState({},"",`/settings/tts?provider=${providerKind==="bailian"?"aliyun-bailian":providerKind}`);
  };
  const closeModal=()=>{setOpen(false);if(location.pathname.startsWith("/settings/"))history.replaceState({},"","/settings")};
  useEffect(()=>{
    if(routeHandled.current)return;
    if(location.pathname==="/settings/tts"){
      const value=new URLSearchParams(location.search).get("provider")||"edge";
      const providerKind=value==="aliyun-bailian"?"bailian":value;
      if(DEFAULTS[providerKind]){routeHandled.current=true;setPanelExpanded(true);configure(providerKind,false)}
    }
  },[db.tts_services.length]);

  const save=async()=>{
    try{
      setBusy("save");
      const result=await api("/api/tts-services",{method:"POST",body:form});
      if(kind==="bailian")await api(`/api/tts-services/${result.id}/voices/sync`,{method:"POST",body:{}});
      await api("/api/settings",{method:"POST",body:extra});
      await refresh();notify("配置已保存");closeModal();
    }catch(error){notify(friendlyError(error))}finally{setBusy("")}
  };
  const testService=async(service=form)=>{
    try{setBusy("test");const result=service?.id?await api(`/api/tts-services/${service.id}/test`,{method:"POST",body:{text:listenText}}):await api("/api/tts-services/test-config",{method:"POST",body:{config:service,text:listenText}});notify(service.provider==="aliyun_bailian"?"连接成功，可以正常使用阿里云百炼配音。":"连接成功，可以正常生成语音。");if(result.voice_url){const audio=new Audio(result.voice_url);audio.play().catch(()=>{})}}
    catch(error){notify(friendlyError(error))}finally{setBusy("")}
  };
  const toggleService=async providerKind=>{const service=serviceFor(providerKind);if(!service)return configure(providerKind);try{await api("/api/tts-services",{method:"POST",body:{...service,api_key:"",is_enabled:!service.is_enabled}});notify(service.is_enabled?"服务已停用":"服务已启用");refresh()}catch(error){notify(friendlyError(error))}};
  const remove=async()=>{if(!form.id||!confirm("确定删除该配音服务？"))return;await api(`/api/tts-services/${form.id}`,{method:"DELETE"});closeModal();notify("配音服务已删除");refresh()};
  const addVoice=async()=>{if(!form.id)return notify("请先保存配置");if(!voiceDraft.name||!voiceDraft.voice_code)return notify("请填写音色名称和音色代码");await api("/api/tts-voices",{method:"POST",body:{...voiceDraft,service_id:form.id}});setVoiceDraft({name:"",voice_code:"",is_default:false});notify("音色已添加");refresh()};
  const chooseCloneSample=async()=>{const result=await api("/api/select-voice-sample",{method:"POST",body:{}});if(result.path)setClone(value=>({...value,sample_path:result.path}))};
  const createClone=async()=>{if(!form.id)return notify("请先保存 MiniMax 配置");if(!clone.name||!clone.sample_path)return notify("请填写音色名称并选择语音样本");try{setBusy("clone");const result=await api(`/api/tts-services/${form.id}/clone`,{method:"POST",body:clone});setClone({name:"",sample_path:"",voice_id:"",preview_text:""});notify(`克隆成功：${result.voice_code}`);refresh()}catch(error){notify(friendlyError(error))}finally{setBusy("")}};

  return <section className={`tts-settings-panel ${expanded?"expanded":"collapsed"}`}>
    <button type="button" className="tts-settings-summary" aria-expanded={expanded} onClick={()=>setPanelExpanded(!expanded)}>
      <div><h2>AI配音</h2><p>当前服务：<b>{currentService?.name||"Edge TTS"}</b><span/>当前音色：<b>{decodeUnicodeOnce(currentVoice?.name)||"自动选择"}</b></p></div>
      <div><em className={currentConfigured?"ready":"idle"}>{currentConfigured?"已配置":"未配置"}</em><i>{expanded?"⌃":"⌄"}</i></div>
    </button>
    <div className="tts-settings-content" aria-hidden={!expanded}><div className="tts-settings-content-inner">
      <div className="tts-current-tools">
        <label>当前配音服务<select value={currentService?.id||""} onChange={async event=>{const service=db.tts_services.find(item=>String(item.id)===event.target.value);if(service)await api("/api/tts-services",{method:"POST",body:{...service,api_key:"",is_default:true}});refresh()}}>{db.tts_services.map(service=><option key={service.id} value={service.id}>{service.name}</option>)}</select></label>
        <label>默认音色<select value={extra.default_tts_voice_id||""} onChange={event=>setExtra({...extra,default_tts_voice_id:event.target.value})}><option value="">自动选择</option>{db.tts_voices.filter(item=>!currentService||Number(item.service_id)===Number(currentService.id)).map(voice=><option key={voice.id} value={voice.id}>{decodeUnicodeOnce(voice.name)}</option>)}</select></label>
        <label>语速<input type="number" min="0.5" max="2" step="0.05" value={extra.tts_speed||"1"} onChange={event=>setExtra({...extra,tts_speed:event.target.value})}/></label>
        <label>音量<input type="number" min="0" max="2" step="0.05" value={extra.tts_volume||"1"} onChange={event=>setExtra({...extra,tts_volume:event.target.value})}/></label>
        <label>输出格式<input readOnly value={(currentService?.response_format||"mp3").toUpperCase()}/></label>
        <Button onClick={()=>configure(PROVIDERS.find(item=>item.provider===currentService?.provider)?.key||"edge")}>服务配置</Button>
        <Button disabled={!currentService} onClick={()=>testService(currentService)}>测试配音</Button>
      </div>
      <div className="tts-quick-list">
        {PROVIDERS.map(provider=>{const service=serviceFor(provider.key);const configured=provider.key==="edge"||Boolean(service?.api_key_configured);const show=Boolean(revealed[provider.key]);return <div className="tts-quick-row" key={provider.key}>
          <div><b>{provider.name}</b><small>{provider.desc}{provider.advanced?" · 高级配置":""}</small></div>
          <div className="tts-key-state"><span>{provider.key==="edge"?"无需密钥":configured?(show?"已配置":"••••••••••••••••"):"尚未配置"}</span>{provider.key!=="edge"&&configured&&<button title="切换状态显示" onClick={()=>setRevealed(value=>({...value,[provider.key]:!value[provider.key]}))}>{show?"隐藏":"查看"}</button>}</div>
          <em className={service?.is_enabled?"ready":"idle"}>{service?.is_enabled?"已启用":configured?"已配置":"未配置"}</em>
          <button className="tts-config-link" onClick={()=>configure(provider.key)}>去配置</button>
        </div>})}
      </div>
    </div></div>

    {open&&<div className="service-modal-mask" onMouseDown={event=>event.target===event.currentTarget&&closeModal()}><div className="service-modal voice-provider-modal">
      <header><div><span>配音服务设置</span><h2>{kind==="bailian"?"阿里云百炼配置":`${PROVIDERS.find(item=>item.key===kind)?.name}配置`}</h2></div><button type="button" onClick={closeModal}>×</button></header>
      <div className="voice-provider-form">
        {kind==="edge"&&<><Field label="默认音色" wide><select value={extra.default_tts_voice_id} onChange={event=>setExtra({...extra,default_tts_voice_id:event.target.value})}><option value="">自动选择</option>{voices.map(voice=><option key={voice.id} value={voice.id}>{decodeUnicodeOnce(voice.name)}</option>)}</select></Field><Field label="语速"><input type="number" min="0.6" max="1.6" step="0.05" value={extra.tts_speed} onChange={event=>setExtra({...extra,tts_speed:event.target.value})}/></Field><Field label="音调"><input type="number" min="-20" max="20" value={extra.tts_pitch} onChange={event=>setExtra({...extra,tts_pitch:event.target.value})}/></Field><Field label="音频格式"><select value={form.response_format||"mp3"} onChange={event=>setForm({...form,response_format:event.target.value})}><option value="mp3">MP3</option><option value="wav">WAV</option></select></Field></>}
        {kind==="minimax"&&<><Field label="API Key" wide><input type="password" placeholder={form.api_key_configured?"已配置，重新输入可覆盖":"请输入 API Key"} value={form.api_key||""} onChange={event=>setForm({...form,api_key:event.target.value})}/></Field><Field label="Group ID" hint="新版接口可留空"><input value={extra.minimax_group_id} onChange={event=>setExtra({...extra,minimax_group_id:event.target.value})}/></Field><Field label="默认音色"><select value={extra.default_tts_voice_id} onChange={event=>setExtra({...extra,default_tts_voice_id:event.target.value})}><option value="">克隆后选择</option>{voices.map(voice=><option key={voice.id} value={voice.id}>{decodeUnicodeOnce(voice.name)}</option>)}</select></Field><Field label="语速"><input type="number" min="0.5" max="2" step="0.05" value={extra.tts_speed} onChange={event=>setExtra({...extra,tts_speed:event.target.value})}/></Field><Field label="音量"><input type="number" min="0" max="2" step="0.05" value={extra.tts_volume} onChange={event=>setExtra({...extra,tts_volume:event.target.value})}/></Field><Field label="音频格式"><select value={form.response_format||"mp3"} onChange={event=>setForm({...form,response_format:event.target.value})}><option value="mp3">MP3</option><option value="wav">WAV</option></select></Field></>}
        {kind==="bailian"&&<><Field label="API Key" wide><input autoFocus type="password" placeholder={form.api_key_configured?"已配置，重新输入可覆盖":"请输入阿里云百炼 API Key"} value={form.api_key||""} onChange={event=>setForm({...form,api_key:event.target.value})}/></Field><Field label="接口地址" wide><input value={form.base_url||""} onChange={event=>setForm({...form,base_url:event.target.value})}/></Field><Field label="模型"><input value={form.model||""} onChange={event=>setForm({...form,model:event.target.value})}/></Field><Field label="默认音色"><select value={extra.default_tts_voice_id||""} onChange={event=>setExtra({...extra,default_tts_voice_id:event.target.value})}><option value="">保存后自动同步</option>{voices.map(voice=><option key={voice.id} value={voice.id}>{decodeUnicodeOnce(voice.name)}</option>)}</select></Field><Field label="音频格式"><select value={form.response_format||"wav"} onChange={event=>setForm({...form,response_format:event.target.value})}><option value="wav">WAV</option><option value="mp3">MP3</option><option value="opus">OPUS</option></select></Field><Field label="采样率"><select value={extra.aliyun_bailian_sample_rate||"24000"} onChange={event=>setExtra({...extra,aliyun_bailian_sample_rate:event.target.value})}>{[16000,22050,24000,44100,48000].map(rate=><option key={rate} value={rate}>{rate} Hz</option>)}</select></Field><Field label="语速"><input type="number" min="0.5" max="2" step="0.05" value={extra.tts_speed} onChange={event=>setExtra({...extra,tts_speed:event.target.value})}/></Field><Field label="音量"><input type="number" min="0" max="2" step="0.05" value={extra.tts_volume} onChange={event=>setExtra({...extra,tts_volume:event.target.value})}/></Field><Field label="启用此服务"><button type="button" className={`provider-switch ${form.is_enabled?"on":""}`} onClick={()=>setForm({...form,is_enabled:!form.is_enabled})}><i/></button></Field></>}
        {(kind==="openai"||kind==="custom")&&<><Field label={kind==="custom"?"服务名称":"API Key"} wide={kind!=="custom"}>{kind==="custom"?<input value={form.name||""} onChange={event=>setForm({...form,name:event.target.value})}/>:<input type="password" placeholder={form.api_key_configured?"已配置，重新输入可覆盖":"请输入 API Key"} value={form.api_key||""} onChange={event=>setForm({...form,api_key:event.target.value})}/>}</Field>{kind==="custom"&&<Field label="API Key" wide><input type="password" placeholder={form.api_key_configured?"已配置，重新输入可覆盖":"请输入 API Key"} value={form.api_key||""} onChange={event=>setForm({...form,api_key:event.target.value})}/></Field>}<Field label="接口地址" wide><input value={form.base_url||""} onChange={event=>setForm({...form,base_url:event.target.value})}/></Field><Field label="模型"><input value={form.model||""} onChange={event=>setForm({...form,model:event.target.value})}/></Field><Field label="默认音色"><select value={extra.default_tts_voice_id} onChange={event=>setExtra({...extra,default_tts_voice_id:event.target.value})}><option value="">使用服务默认音色</option>{voices.map(voice=><option key={voice.id} value={voice.id}>{decodeUnicodeOnce(voice.name)}</option>)}</select></Field><Field label="音频格式"><select value={form.response_format||"mp3"} onChange={event=>setForm({...form,response_format:event.target.value})}><option value="mp3">MP3</option><option value="wav">WAV</option><option value="opus">OPUS</option><option value="aac">AAC</option></select></Field></>}
        <Field label="试听文案" wide><input value={listenText} onChange={event=>setListenText(event.target.value)}/></Field>
      </div>
      {form.id&&<div className="voice-config-collapse"><button type="button" onClick={()=>setVoicesOpen(value=>!value)}><span><b>音色管理</b><small>{voices.length} 个已保存音色</small></span><em>{voicesOpen?"收起":"展开"}</em></button>{voicesOpen&&<div className="voice-config-collapse-body"><div className="voice-chips">{voices.map(voice=><span key={voice.id}>{decodeUnicodeOnce(voice.name)}<small>{kind==="custom"?voice.voice_code:"已保存音色"}</small><button type="button" onClick={async()=>{await api(`/api/tts-voices/${voice.id}`,{method:"DELETE"});refresh()}}>×</button></span>)}</div>{["openai","custom"].includes(kind)&&<div className="voice-add"><input placeholder="音色名称" value={voiceDraft.name} onChange={event=>setVoiceDraft({...voiceDraft,name:event.target.value})}/><input placeholder="音色标识" value={voiceDraft.voice_code} onChange={event=>setVoiceDraft({...voiceDraft,voice_code:event.target.value})}/><Button onClick={addVoice}>添加音色</Button></div>}</div>}</div>}
      {kind==="minimax"&&form.id&&<div className="voice-config-collapse"><button type="button" onClick={()=>setCloneOpen(value=>!value)}><span><b>真实语音克隆</b><small>上传本人录音，创建真人音色</small></span><em>{cloneOpen?"收起":"展开"}</em></button>{cloneOpen&&<div className="voice-config-collapse-body clone-config-body"><Field label="音色名称"><input placeholder="例如 老板本人" value={clone.name} onChange={event=>setClone({...clone,name:event.target.value})}/></Field><Field label="Voice ID" hint="可留空，系统自动生成"><input value={clone.voice_id} onChange={event=>setClone({...clone,voice_id:event.target.value})}/></Field><Field label="语音样本" wide><div className="listen-row"><input readOnly placeholder="10秒至5分钟，MP3/M4A/WAV" value={clone.sample_path}/><Button onClick={chooseCloneSample}>选择文件</Button></div></Field><Field label="试听文案" wide><input value={clone.preview_text} onChange={event=>setClone({...clone,preview_text:event.target.value})}/></Field><div className="clone-submit"><Button primary disabled={busy==="clone"} onClick={createClone}>{busy==="clone"?"正在克隆":"开始克隆"}</Button></div></div>}</div>}
      <footer>{form.id&&kind!=="edge"&&<Button danger onClick={remove}>删除服务</Button>}<span/><Button onClick={closeModal}>取消</Button><Button disabled={busy==="test"} onClick={()=>testService()}>测试连接</Button><Button disabled={busy==="test"} onClick={()=>testService()}>试听语音</Button><Button primary disabled={busy==="save"} onClick={save}>保存配置</Button></footer>
    </div></div>}
  </section>;
}
