$src = "E:\roteirizador"
$dst = "E:\roteirizador\roteirizador\data\fdb"
New-Item -ItemType Directory -Force $dst | Out-Null
Copy-Item "$src\cliente locação.FDB"           "$dst\locacao.fdb"           -Force
Copy-Item "$src\cliente entrega posterior.FDB" "$dst\entrega_posterior.fdb" -Force
Get-ChildItem $dst | Select-Object Name, @{n='GB';e={[math]::Round($_.Length/1GB,2)}}
