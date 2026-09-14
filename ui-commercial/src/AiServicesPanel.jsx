import React, {useEffect, useRef, useState} from "react";

const EMPTY={name:"AI 服务",provider:"openai_compatible",base_url:"",api_key:"",model:"",timeout_seconds:60,is_enabled:true,is_default:false,system_prompt:""};
const BAILIAN={...EMPTY,name:"阿里云百炼 AI",base_url:"https://dashscope.aliyuncs.com/compatible-mode/v1",model:"qwen-plus"};

export default function AiServicesPanel({db,refresh,notify,request,Button}){
  const [open,setOpen]=useState(false);
  const [form,setForm]=useState(EMPTY);
  const [revealed,setRevealed]=useState({});
  const [testing,setTesting]=useState(false);
  const [testResult,setTestResult]=useState(null);
  const routeHandled=useRef(false);
  const bailian=db.ai_services.find(item=>String(item.name||"").includes("百炼")||String(item.base_url||"").includes("dashscope.aliyuncs.com/compatible-mode"));
  const regular=db.ai_services.filter(item=>item.id!==bailian?.id);
  const edit=(value,pushRoute=true)=>{setForm({...value,api_key:""});setTestResult(null);setOpen(true);if(pushRoute)history.pushState({},"",`/settings/ai?provider=${value===bailian||String(value.name).includes("百炼")?"aliyun-bailian":"current"}`)};
  const close=()=>{if(testing)return;setOpen(false);setTestResult(null);if(location.pathname.startsWith("/settings/"))history.replaceState({},"","/settings")};
  useEffect(()=>{
    if(routeHandled.current||location.pathname!=="/settings/ai")return;
    const provider=new URLSearchParams(location.search).get("provider");
    routeHandled.current=true;
    edit(provider==="aliyun-bailian"?{...BAILIAN,...(bailian||{})}:{...EMPTY,...(db.ai_services[0]||{})},false);
  },[db.ai_services.length]);
  const save=async()=>{try{const result=await request("/api/ai-services",{method:"POST",body:form});await refresh();notify("AI 服务配置已保存");setForm(value=>({...value,id:result.id,api_key:""}));close()}catch(error){notify("保存失败："+error.message)}};
  const test=async()=>{if(testing)return;setTesting(true);setTestResult(null);try{const result=await request("/api/ai-services/test-config",{method:"POST",body:{config:form}});setTestResult({ok:true,...result});notify("AI 服务连接成功")}catch(error){const actions={AI_CONNECTION_FAILED:"请确认接口地址正确，或启动对应的本地服务。",AI_TIMEOUT:"请检查服务状态、网络或稍后重试。",AI_AUTH_FAILED:"请重新输入有效的 API Key。",AI_MODEL_NOT_FOUND:"请检查当前模型名称。",AI_INVALID_RESPONSE:"请检查接口是否兼容 OpenAI 格式。",AI_PROVIDER_ERROR:"请检查服务端状态和接口地址。"};setTestResult({ok:false,code:error.code||"AI_PROVIDER_ERROR",message:error.message,action:actions[error.code]||"请检查配置后重试。"})}finally{setTesting(false)}};
  const openBailianKey=async()=>{try{await request("/api/ai-services/open-bailian-key",{method:"POST",body:{}});notify("已打开阿里百炼 API Key 页面")}catch(error){notify("无法打开阿里百炼页面："+(error.message||"请检查默认浏览器"))}};
  const remove=async()=>{if(!form.id||!confirm("确定删除该 AI 服务？"))return;await request(`/api/ai-services/${form.id}`,{method:"DELETE"});close();notify("AI 服务已删除");refresh()};
  const rows=[...(regular.length?regular:[{...EMPTY,name:"AI 模型服务",virtual:true}]),{...BAILIAN,...(bailian||{}),name:"阿里云百炼 AI",bailian:true}];
  return <section className="ai-quick-center">
    <header><div><h2>AI配置</h2><p>文案改写与内容生成服务</p></div><Button onClick={()=>edit({...EMPTY,name:"自定义 AI 服务"})}>新增服务</Button></header>
    <div className="ai-quick-list">{rows.map((service,index)=>{const configured=Boolean(service.api_key_configured);const key=service.id||`${service.name}-${index}`;const show=Boolean(revealed[key]);return <div className="ai-quick-row" key={key}>
      <label>{service.name}</label><div className="ai-key-display"><span>{configured?(show?"已配置":"••••••••••••••••"):"尚未配置"}</span>{configured&&<button title="切换状态显示" onClick={()=>setRevealed(value=>({...value,[key]:!value[key]}))}>{show?"隐藏":"查看"}</button>}</div><em className={service.is_enabled&&configured?"ready":"idle"}>{service.is_enabled&&configured?"已启用":configured?"已配置":"未配置"}</em><button className="ai-config-link" onClick={()=>edit(service.bailian?{...BAILIAN,...(bailian||{})}:service)}>去配置</button>
    </div>})}</div>
    {open&&<div className="service-modal-mask" onMouseDown={event=>event.target===event.currentTarget&&close()}><div className="service-modal ai-compact-modal"><header><div><span>AI 服务设置</span><h2>{String(form.name||"").includes("百炼")?"阿里云百炼 AI 配置":form.id?"配置 AI 服务":"新增 AI 服务"}</h2></div><button type="button" disabled={testing} onClick={close}>×</button></header><div className="service-form"><label>服务名称<input value={form.name||""} onChange={event=>setForm({...form,name:event.target.value})}/></label><label>模型<input value={form.model||""} onChange={event=>setForm({...form,model:event.target.value})}/></label><label className="wide">接口地址<input value={form.base_url||""} onChange={event=>setForm({...form,base_url:event.target.value})}/></label><label className="wide">API Key<div className="ai-key-input-row"><input type="password" autoFocus placeholder={form.api_key_configured?"已配置，重新输入可覆盖":"请输入 API Key"} value={form.api_key||""} onChange={event=>setForm({...form,api_key:event.target.value})}/>{String(form.name||"").includes("百炼")&&<button type="button" title="前往阿里百炼获取 API Key" onClick={openBailianKey}>获取KEY</button>}</div></label><label>超时（秒）<input type="number" value={form.timeout_seconds||60} onChange={event=>setForm({...form,timeout_seconds:Number(event.target.value)})}/></label><label className="check-label"><input type="checkbox" checked={!!form.is_enabled} onChange={event=>setForm({...form,is_enabled:event.target.checked})}/>启用此服务</label><label className="check-label"><input type="checkbox" checked={!!form.is_default} onChange={event=>setForm({...form,is_default:event.target.checked})}/>设为默认</label><label className="wide">系统提示词<textarea value={form.system_prompt||""} onChange={event=>setForm({...form,system_prompt:event.target.value})}/></label></div>{testResult&&<div className={`ai-test-result ${testResult.ok?"success":"error"}`}><strong>{testResult.ok?"连接成功":"连接失败"}</strong>{testResult.ok?<><span>服务：{testResult.service}</span><span>模型：{testResult.model}</span><span>响应时间：{testResult.latency_ms}ms</span><span>模型验证：{testResult.model_validation}</span></>:<><span>原因：{testResult.message}</span><span>处理方式：{testResult.action}</span><small>错误码：{testResult.code}</small></>}</div>}<footer>{form.id&&<Button disabled={testing} onClick={remove}>删除服务</Button>}<span/><Button disabled={testing} onClick={close}>取消</Button><Button disabled={testing} onClick={test}>{testing?"测试中...":"测试连接"}</Button><Button primary disabled={testing} onClick={save}>保存配置</Button></footer></div></div>}
  </section>;
}

