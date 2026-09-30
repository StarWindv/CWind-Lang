package main

import (
	"fmt"
	"os"
	"strconv"
)

func main() {
	total, _ := strconv.ParseUint(os.Args[1], 10, 64)

	var a, b uint64 = 0, 1
	boxv := make([]*uint64, 0, total)

	for count := uint64(0); count < total; count++ {
		t := new(uint64)
		*t = a + b
		a = b
		b = *t
		boxv = append(boxv, t)
	}

	fmt.Println(a)
}
