"use strict";
const main=document.querySelector("main"),prefix=main.dataset.prefix,id=main.dataset.id;
const token=document.querySelector("#token"),status=document.querySelector("#status");
const fragment=new URLSearchParams(location.hash.slice(1));
token.value=fragment.get("this-is-unsafe-dont-do-this-auth")||fragment.get("token")||"";
history.replaceState(null,"",location.pathname);
class TransferError extends Error{}
let busy=false,uploadFinished=false;
function completedUpload(){
 uploadFinished=true;token.value="";token.disabled=true;
 document.querySelector("#file").disabled=true;
 const button=document.querySelector("#upload");button.disabled=true;button.textContent="Uploaded";
 status.textContent="Uploaded. Your file was sent successfully. To send another file, ask for a new upload link.";
}
async function act(fn){
 if(busy)return;busy=true;document.querySelectorAll("button").forEach(b=>b.disabled=true);
 try{await fn();}catch(e){status.textContent=e instanceof TransferError?e.message:
  (document.querySelector("#upload")?
   "Could not reach Fishmarket. Your upload may already have completed. Check with the recipient before trying again.":
   "Could not reach Fishmarket. Try again, or ask for a new download link.");}
 finally{busy=false;document.querySelectorAll("button").forEach(b=>b.disabled=uploadFinished);}
}
async function checked(response,upload=false){
 const body=await response.json();
 if(!response.ok)throw new TransferError(upload && [403,404].includes(response.status)?
 "This upload link has been used or has expired. Ask for a new upload link.":body.message||"Transfer unavailable.");
 return body;
}
const downloadButton=document.querySelector("#download");
if(downloadButton)downloadButton.onclick=()=>act(async()=>{
 status.textContent="Claiming your download…";
 const result=await checked(await fetch(prefix+"/api/"+id+"/claim",{method:"POST",
 headers:{"Content-Type":"application/json"},body:JSON.stringify({token:token.value})}));
 status.textContent="Download started. Keep this tab to retry within ten minutes.";
 const link=document.createElement("a");link.href=result.download_url;link.download="";link.click();
});
const uploadButton=document.querySelector("#upload");
if(uploadButton)uploadButton.onclick=()=>act(async()=>{
 const file=document.querySelector("#file").files[0];
 if(!file)throw new TransferError("Choose a file first.");
 status.textContent="Uploading…";
 await checked(await fetch(prefix+"/api/"+id+"/upload",{method:"PUT",
 headers:{"X-Fishmarket-Token":token.value,"X-Fishmarket-Filename":encodeURIComponent(file.name)},
 body:file}),true);
 completedUpload();
 try{sessionStorage.setItem("fishmarket:uploaded:"+id,"1");}catch(e){}
});

if(uploadButton){
 try{if(sessionStorage.getItem("fishmarket:uploaded:"+id)==="1")completedUpload();}catch(e){}
}
