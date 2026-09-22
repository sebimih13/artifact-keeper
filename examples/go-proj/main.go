package main

import (
	"fmt"
	"log"

	"google.golang.org/genproto/googleapis/api/annotations"
	"google.golang.org/protobuf/proto"
)

func main() {
	rule := &annotations.HttpRule{
		Selector: "example.Greeter.SayHello",
		Pattern:  &annotations.HttpRule_Get{Get: "/hello/{name}"},
	}

	encoded, err := proto.Marshal(rule)
	if err != nil {
		log.Fatal(err)
	}
	var decoded annotations.HttpRule
	if err := proto.Unmarshal(encoded, &decoded); err != nil {
		log.Fatal(err)
	}
	if !proto.Equal(rule, &decoded) {
		log.Fatal("protobuf round-trip mismatch")
	}

	fmt.Println("Protobuf round-trip successful")
	fmt.Printf("Selector: %s\nGET: %s\nEncoded size: %d bytes\n", decoded.GetSelector(), decoded.GetGet(), len(encoded))
}
