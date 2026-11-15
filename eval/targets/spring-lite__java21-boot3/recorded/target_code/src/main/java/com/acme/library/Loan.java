package com.acme.library;

import jakarta.persistence.*;
import java.time.LocalDate;

@Entity
public class Loan {
    @Id @GeneratedValue
    private Long id;
    @ManyToOne
    private Book book;
    @ManyToOne
    private Member member;
    private LocalDate dueDate;
    private LocalDate returnedOn;

    public void setDueDate(LocalDate d) { this.dueDate = d; }
    // Q-2 (fix): expose the due date so a successful checkout serialises (was HTTP 500)
    public LocalDate getDueDate() { return dueDate; }
}
