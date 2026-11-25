package main

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestHarvestBing(t *testing.T) {
	h := NewHarvester("Acme Corp", false)
	page := `
<ol>
  <li class="b_algo"><h2><a>Jane Smith - Engineer at Acme Corp | LinkedIn</a></h2>
    <cite>https://www.linkedin.com/in/janesmith</cite></li>
  <li class="b_algo"><h2><a>Acme Corp | LinkedIn</a></h2>
    <cite>https://www.linkedin.com/company/acme</cite></li>
  <li class="b_algo"><h2><a>Bob Jones - CFO - Acme Corp</a></h2>
    <cite>https://au.linkedin.com/in/bobjones</cite></li>
</ol>`
	added := h.Capture("https://www.bing.com/search?q=x", page)
	if added != 2 {
		t.Fatalf("added = %d, want 2", added)
	}
	got := h.Names()
	want := []string{"Bob Jones", "Jane Smith"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("names = %v, want %v", got, want)
	}
}

func TestHarvestGoogle(t *testing.T) {
	h := NewHarvester("Acme Corp", false)
	page := `
<div class="g"><div><span>LinkedIn</span></div><h3>Carol White - VP at Acme Corp</h3></div>
<div class="g"><div><span>example.com</span></div><h3>Acme Corp Official Website</h3></div>
<div class="g"><div><span>Crunchbase</span></div><h3>Dave Brown profile - Crunchbase</h3></div>`
	added := h.Capture("https://www.google.com/search?q=y", page)
	if added != 1 {
		t.Fatalf("added = %d, want 1 (only the LinkedIn-labelled result)", added)
	}
	if got := h.Names(); !reflect.DeepEqual(got, []string{"Carol White"}) {
		t.Fatalf("names = %v, want [Carol White]", got)
	}
}

func TestSafeMode(t *testing.T) {
	h := NewHarvester("Acme Corp", true) // safe: drop titles without the company
	page := `
<li class="b_algo"><h2>Zed Alpha - Engineer at OtherCo | LinkedIn</h2>
  <cite>linkedin.com/in/zed</cite></li>
<li class="b_algo"><h2>Amy Lee - Dev at Acme Corp | LinkedIn</h2>
  <cite>linkedin.com/in/amy</cite></li>`
	h.Capture("https://www.bing.com/search", page)
	if got := h.Names(); !reflect.DeepEqual(got, []string{"Amy Lee"}) {
		t.Fatalf("names = %v, want [Amy Lee]", got)
	}
}

func TestNameChunk(t *testing.T) {
	cases := map[string]string{
		// Keep the WHOLE name, including middle names and post-nominals that
		// sit before a comma.
		"Snezana Bakalovski MAICD - Chief People Officer":   "Snezana Bakalovski MAICD",
		"Mary Jane Watson - UX Designer, MBA | LinkedIn":    "Mary Jane Watson",
		"Jane Smith - Head of Marketing at Acme | LinkedIn": "Jane Smith",
		// Comma drops credentials (regression: used to become "John CPA").
		"John Smith, CPA - Finance at Acme": "John Smith",
		// Wrappers, brackets, en-dash, and non-Latin scripts.
		"View Bob Jones’s profile":      "Bob Jones",
		"Jane Doe (She/Her) - Designer": "Jane Doe",
		"Ana García Pérez – Ingeniera":  "Ana García Pérez", // en-dash separator
		"John Doe | LinkedIn":           "John Doe",
		"Пётр Иванов - Инженер":          "Пётр Иванов",       // Cyrillic survives
	}
	for in, want := range cases {
		if got := nameChunk(in); got != want {
			t.Errorf("nameChunk(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestFmtName(t *testing.T) {
	cases := []struct{ first, last, format, want string }{
		{"Jane", "Smith", "{first}.{last}@acme.com", "jane.smith@acme.com"},
		{"Bob", "Jones", "{f}{last}@acme.com", "bjones@acme.com"},
		{"Carol", "White", "{first}{l}", "carolw"},
		{"", "Nobody", "{first}.{last}", ""}, // empty first -> empty
	}
	for _, c := range cases {
		if got := fmtName(c.first, c.last, c.format); got != c.want {
			t.Errorf("fmtName(%q,%q,%q) = %q, want %q",
				c.first, c.last, c.format, got, c.want)
		}
	}
}

func TestFlushNamesAtomic(t *testing.T) {
	h := NewHarvester("Acme", false)
	h.Capture("https://www.bing.com/s?q=x",
		`<li class="b_algo"><h2>Jane Smith - Eng | LinkedIn</h2><cite>linkedin.com/in/j</cite></li>`)
	dir := t.TempDir()
	out := filepath.Join(dir, "names.txt")
	h.FlushNames(out)
	data, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "Jane Smith\n" {
		t.Fatalf("file = %q, want %q", data, "Jane Smith\n")
	}
	if _, err := os.Stat(out + ".tmp"); !os.IsNotExist(err) {
		t.Fatalf("leftover .tmp file should not exist")
	}
	// empty harvester writes an empty file, no trailing junk
	empty := NewHarvester("x", false)
	out2 := filepath.Join(dir, "empty.txt")
	empty.FlushNames(out2)
	if data, _ := os.ReadFile(out2); string(data) != "" {
		t.Fatalf("empty file = %q, want empty", data)
	}
}
