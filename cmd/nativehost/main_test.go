package main

import (
	"path/filepath"
	"reflect"
	"testing"
)

func TestBuildCommandUsesRepositoryRootAndForwardsChromeArguments(t *testing.T) {
	root := filepath.Join("somewhere", "ExternalCall")
	executable := filepath.Join(root, "bin", "externalcall-host")
	chromeArguments := []string{"chrome-extension://abcdefghijklmnop/", "--parent-window=10"}

	directory, arguments, err := buildCommand(executable, "python3", chromeArguments)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{
		"-m", "companion.native_host", "--root", root,
		"chrome-extension://abcdefghijklmnop/", "--parent-window=10",
	}
	if directory != root || !reflect.DeepEqual(arguments, want) {
		t.Fatalf("directory=%q arguments=%q, want directory=%q arguments=%q", directory, arguments, root, want)
	}
}
