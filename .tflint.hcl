# tflint settings (run by .github/workflows/lint.yml as `tflint --recursive`).
plugin "terraform" {
  enabled = true
  preset  = "recommended"
}
