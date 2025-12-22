# Outputs for Karb infrastructure

output "bot_public_ip" {
  description = "Public IP of the bot server (us-east-1)"
  value       = aws_instance.bot.public_ip
}

output "bot_private_ip" {
  description = "Private IP of the bot server"
  value       = aws_instance.bot.private_ip
}

output "proxy_public_ip" {
  description = "Public IP of the proxy server (ca-central-1)"
  value       = aws_instance.proxy.public_ip
}

output "proxy_private_ip" {
  description = "Private IP of the proxy server"
  value       = aws_instance.proxy.private_ip
}

output "proxy_socks5_address" {
  description = "SOCKS5 proxy address for bot configuration"
  value       = "${aws_instance.proxy.public_ip}:1080"
}

output "ssh_bot" {
  description = "SSH command for bot server"
  value       = "ssh ubuntu@${aws_instance.bot.public_ip}"
}

output "ssh_proxy" {
  description = "SSH command for proxy server"
  value       = "ssh ubuntu@${aws_instance.proxy.public_ip}"
}

output "ansible_inventory" {
  description = "Ansible inventory snippet"
  value       = <<-EOT
    [bot]
    ${aws_instance.bot.public_ip}

    [proxy]
    ${aws_instance.proxy.public_ip}
  EOT
}

output "dashboard_url" {
  description = "Dashboard URL"
  value       = "http://karb.arkets.com"
}

output "dns_record" {
  description = "DNS record created"
  value       = "karb.arkets.com -> ${aws_instance.bot.public_ip}"
}
