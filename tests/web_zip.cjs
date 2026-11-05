// Validate the browser ZIP writer without a browser or network dependencies.
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {hidden:true, classList:{toggle(){},add(){},remove(){}}, addEventListener(){}, setAttribute(){}, append(){},replaceChildren(){},querySelector(){return element("button");}});
  return elements.get(id);
}
const context = vm.createContext({
  document:{getElementById:element,querySelectorAll:()=>[],hidden:false},
  location:{host:"127.0.0.1:8765"}, Blob, File, TextEncoder, Uint8Array, Uint32Array, DataView, URLSearchParams,
  fetch:async()=>({ok:false,status:401,json:async()=>({error:"请先登录"})}),
  setInterval(){},setTimeout(){},clearTimeout(){},confirm:()=>false,
});
vm.runInContext(fs.readFileSync(process.argv[2],"utf8"), context);
const first = new File(["中文内容\n"], "hello.txt");
Object.defineProperty(first,"webkitRelativePath",{value:"folder/子目录/hello.txt"});
const empty = new File([],"empty.txt");
Object.defineProperty(empty,"webkitRelativePath",{value:"folder/empty.txt"});
const large = new File([new Uint8Array(1024*1024+17).fill(37)],"large.bin");
Object.defineProperty(large,"webkitRelativePath",{value:"folder/large.bin"});
context.testFiles = [first,empty,large];
(async()=>{
  const blob = await vm.runInContext("settings={max_file_bytes:10*1024*1024};zipDirectory(testFiles)",context);
  fs.writeFileSync(process.argv[3], Buffer.from(await blob.arrayBuffer()));
  await assert.rejects(vm.runInContext("cancelled=true;zipDirectory(testFiles)",context), /取消/);
})().catch(error=>{console.error(error);process.exitCode=1;});
