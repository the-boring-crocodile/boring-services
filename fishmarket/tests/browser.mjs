import fs from "node:fs";
import path from "node:path";
import {createRequire} from "node:module";
const require=createRequire(import.meta.url);
const {firefox}=require(process.env.PLAYWRIGHT_MODULE);
const fixture=JSON.parse(fs.readFileSync(process.argv[2],"utf8"));
let browser;let phase="startup";
try{
 browser=await firefox.launch({headless:true});
 const context=await browser.newContext({acceptDownloads:true});
 const page=await context.newPage();
 const violations=[];
 await page.addInitScript(()=>{window.cspViolations=[];document.addEventListener("securitypolicyviolation",e=>window.cspViolations.push(e.violatedDirective));});
 page.on("console",m=>{if(m.type()==="error")violations.push(m.text().includes("Content-Security-Policy")?"csp":"other");});
 phase="download-page";await page.goto(fixture.download.open_url);
 if(page.url().includes("#"))throw Error("fragment not cleared");
 if(await page.locator("#upload,#file").count())throw Error("upload controls on download page");
 const promise=page.waitForEvent("download");
 await page.getByRole("button",{name:"Download",exact:true}).click();
 phase="download-save";const download=await promise;
 await download.saveAs(path.join(fixture.directory,"browser-result.bin"));
 if((await download.failure())!==null)throw Error("download failed");
 await page.getByRole("button",{name:"Download",exact:true}).click();
 await page.waitForTimeout(150);
 phase="foreign-browser";const other=await browser.newContext();
 const second=await other.newPage();
 await second.goto(fixture.download.open_url);
 await second.getByRole("button",{name:"Download",exact:true}).click();
 await second.waitForFunction(()=>document.querySelector("#status").textContent.includes("already claimed"));
 await other.close();
 phase="upload";await page.goto(fixture.upload.open_url.replace("#this-is-unsafe-dont-do-this-auth=","#token="));
 if(await page.locator("#download").count())throw Error("download control on upload page");
 if(page.url().includes("#"))throw Error("upload fragment not cleared");
 await page.locator("#file").setInputFiles(path.join(fixture.directory,"browser-source.bin"));
 await page.getByRole("button",{name:"Upload",exact:true}).click();
 await page.waitForFunction(()=>document.querySelector("#status").textContent.startsWith("Uploaded."));
 if(!(await page.getByRole("button",{name:"Uploaded",exact:true}).isDisabled()))throw Error("completed upload still enabled");
 await page.reload();
 if(!(await page.getByRole("button",{name:"Uploaded",exact:true}).isDisabled()))throw Error("completed upload lost after reload");
 phase="console";if(violations.length){console.error(JSON.stringify({consoleCategories:violations,cspDirectives:await page.evaluate(()=>window.cspViolations)}));throw Error("browser console/CSP error");}
 await context.close();
 console.log("PASS: Firefox fragment cleanup, explicit claim/download/retry, foreign browser refusal, file upload and CSP");
}catch(e){
 console.error("FAIL: Firefox rehearsal phase="+phase+" ("+e.constructor.name+"); private details suppressed");
 process.exitCode=1;
}finally{if(browser)await browser.close();}
