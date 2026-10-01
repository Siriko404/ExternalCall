package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
)

func buildCommand(executablePath, pythonPath string, chromeArguments []string) (string, []string, error) {
	if executablePath == "" || pythonPath == "" {
		return "", nil, errors.New("executable and Python paths are required")
	}
	root := filepath.Dir(filepath.Dir(filepath.Clean(executablePath)))
	arguments := []string{"-m", "companion.native_host", "--root", root}
	arguments = append(arguments, chromeArguments...)
	return root, arguments, nil
}

func main() {
	executable, err := os.Executable()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	root := filepath.Dir(filepath.Dir(filepath.Clean(executable)))
	python := filepath.Join(root, ".venv", "bin", "python")
	if _, statErr := os.Stat(python); statErr != nil {
		python = filepath.Join(root, ".venv", "Scripts", "python.exe")
	}
	if _, statErr := os.Stat(python); statErr != nil {
		python, err = exec.LookPath("python3")
		if err != nil {
			python, err = exec.LookPath("python")
		}
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "Python was not found on PATH")
		os.Exit(1)
	}
	directory, arguments, err := buildCommand(executable, python, os.Args[1:])
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	command := exec.Command(python, arguments...)
	command.Dir = directory
	command.Stdin = os.Stdin
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	if err := command.Run(); err != nil {
		var exitError *exec.ExitError
		if errors.As(err, &exitError) {
			os.Exit(exitError.ExitCode())
		}
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
