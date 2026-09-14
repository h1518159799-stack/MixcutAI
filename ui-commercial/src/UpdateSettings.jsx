import React,{useEffect,useState} from "react";

async function api(path,options={}){const response=await fetch(path,{headers:{"Content-Type":"application/json"},...options,body:options.body&&typeof options.body!=="string"?JSON.stringify(options.body):options.body});const json=await response.json();if(!json.ok)throw Error(json.error||"操作失败");return json.data}

export default function UpdateSettings({db,refresh,notify,Button,Icon}){
  const [status,setStatus]=useState({current_version:"1.2.0",auto_check_update:true,configured:false});
  const [checking,setChecking]=useState(false),[updating,setUpdating]=useState(false),[updateMessage,setUpdateMessage]=useState(""),[result,setResult]=useState(null);
  useEffect(()=>{let mounted=true;api("/api/update/status").then(data=>mounted&&setStatus(data)).catch(()=>{});return()=>{mounted=false}},[]);
  useEffect(()=>setStatus(current=>({...current,auto_check_update:String(db.settings.auto_check_update??"1")!=="0"})),[db.settings.auto_check_update]);
  const toggleAuto=async()=>{const enabled=!status.auto_check_update;setStatus(current=>({...current,auto_check_update:enabled}));try{await api("/api/settings",{method:"POST",body:{auto_check_update:enabled?"1":"0"}});await refresh(false);notify(enabled?"已开启自动检查更新":"已关闭自动检查更新")}catch(error){setStatus(current=>({...current,auto_check_update:!enabled}));notify(error.message)}};
  const check=async()=>{setChecking(true);try{const data=await api("/api/update/check",{method:"POST",body:{}});if(data.available)setResult(data);else notify(`当前 v${data.current_version} 已是最新版本`)}catch(error){notify(error.message)}finally{setChecking(false)}};
  useEffect(()=>{if(!updating)return;let active=true;const poll=async()=>{try{const data=await api("/api/update/status");if(!active)return;const process=data.update_process||{};setUpdateMessage(process.message||"准备更新...");if(process.state==="failed"){setUpdating(false);notify(process.error||"更新失败")}}catch(_error){}};poll();const timer=setInterval(poll,500);return()=>{active=false;clearInterval(timer)}},[updating]);
  const apply=async()=>{setUpdating(true);setUpdateMessage("准备更新...");try{const data=await api("/api/update/apply",{method:"POST",body:{}});setUpdateMessage(data.message||"准备更新...")}catch(error){setUpdating(false);notify(error.message)}};
  const manifest=result?.manifest;
  return <>
    <section className="update-settings-card">
      <header><div><i><Icon name="replace" size={18}/></i><span><b>在线更新</b><small>安全下载、校验并自动替换主程序</small></span></div><em>{status.configured?"UPDATE READY":"SERVER NOT SET"}</em></header>
      <div className="update-settings-body">
        <div><span>当前版本</span><strong>v{status.current_version}</strong><small>Mixcut AI Windows</small></div>
        <div><span>自动检查更新</span><button className={`switch ${status.auto_check_update?"on":""}`} onClick={toggleAuto}><i/></button><small>授权验证成功后检查</small></div>
        <div><span>更新服务器</span><strong className={status.configured?"connected":"not-configured"}>{status.configured?"已配置 HTTPS":"尚未配置"}</strong><small>由软件发行方统一配置</small></div>
        <Button quiet icon="replace" disabled={checking||!status.configured} onClick={check}>{checking?"正在检查":"检查更新"}</Button>
      </div>
      <footer><Icon name="check" size={14}/><span>新版下载后会进行 SHA256 校验，并在替换前备份用户数据库。</span></footer>
    </section>
    {manifest?<div className="update-dialog-mask" role="dialog" aria-modal="true"><section className="update-dialog"><header><i><Icon name="replace" size={22}/></i><div><span>{manifest.force_update?"REQUIRED UPDATE":"NEW VERSION"}</span><h2>Mixcut AI v{manifest.version}</h2></div>{!manifest.force_update?<button onClick={()=>setResult(null)}>×</button>:null}</header><div><strong>v{result.current_version} <em>→</em> v{manifest.version}</strong><p>{manifest.release_notes||"此版本包含功能优化和稳定性改进。"}</p><ul><li>HTTPS安全下载</li><li>SHA256完整性校验</li><li>数据库与旧EXE自动备份</li><li>失败自动恢复并启动旧版</li></ul></div><footer>{!manifest.force_update?<Button quiet disabled={updating} onClick={()=>setResult(null)}>稍后更新</Button>:null}<Button primary icon="replace" disabled={updating} onClick={apply}>{updating?(updateMessage||"准备更新..."):"立即更新"}</Button></footer></section></div>:null}
  </>;
}
